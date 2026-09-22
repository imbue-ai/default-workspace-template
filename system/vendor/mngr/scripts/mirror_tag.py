"""Re-point private ``minds-v*`` release tags onto the public mirror.

Copybara exports commits, not tags: a ``minds-v*`` tag names an older ``main``
SHA (the verified GREEN_MNGR_SHA), which the per-change ``tag_name`` the
destination supports cannot express. Copybara migrates ``main``'s first-parent
commits, and every exported public commit carries a
``GitOrigin-RevId: <private-sha>`` trailer, so a private tag maps to a public
commit in two steps. First find the oldest first-parent commit of ``main`` that
contains the tagged commit: the tagged commit itself, or -- a release lands by
``--no-ff`` merge and the tag names the merge parent, so the tag is often off
the chain -- the merge that landed it. Then walk ``main``'s first-parent
history from there until a commit some public commit was exported from. A
private-only commit (one that touched no allowlisted path) produced no public
commit, so its nearest exported first-parent ancestor carries the same public
tree.

Usage:
    uv run --package imbue-common python scripts/mirror_tag.py --tag minds-v0.5.2 --dry-run
    uv run --package imbue-common python scripts/mirror_tag.py --all-missing --dry-run
    MIRROR_PUSH_TOKEN=... uv run --package imbue-common python scripts/mirror_tag.py --tag minds-v0.5.2

The mirror push runs after this repo's ``main`` moves, so a tag pushed right
after its commit lands can race the export. The script waits until the newest
exported private SHA is at or past the tagged commit before it maps anything.
The private checkout is read once, so an export of a commit that landed after
it (the mirror running past everything the checkout knows) counts as caught up.
That test cannot tell a lagging mirror from one that ran and had nothing to
export: a tag whose landing commit and every later first-parent commit are
private-only waits out the deadline, and resolves once any later commit is
exported. ``minds-v*`` tags name version bumps that touch public paths, so
that case is not expected in practice.
"""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import SecretStr
from tenacity import RetryCallState
from tenacity import Retrying
from tenacity import retry_if_exception_type
from tenacity import wait_fixed

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

PUBLIC_REPO_URL: Final[str] = "https://github.com/imbue-ai/mngr.git"
PUBLIC_PUSH_URL_TEMPLATE: Final[str] = "https://x-access-token:{token}@github.com/imbue-ai/mngr.git"
PUSH_TOKEN_ENV_VAR: Final[str] = "MIRROR_PUSH_TOKEN"
# Only this branch is exported, so only tags reachable from it have a public counterpart.
MIRRORED_BRANCH: Final[str] = "main"
DEFAULT_MIRRORED_REF: Final[str] = "origin/main"
MIRRORED_TAG_GLOB: Final[str] = "minds-v*"
ORIGIN_TRAILER_KEY: Final[str] = "GitOrigin-RevId"
# Committer identity for the public tag objects; matches the mirror-push bot.
TAG_COMMITTER_NAME: Final[str] = "imbue-codesync[bot]"
TAG_COMMITTER_EMAIL: Final[str] = "308188143+imbue-codesync[bot]@users.noreply.github.com"
DEFAULT_CATCH_UP_TIMEOUT_SECONDS: Final[float] = 1800.0
CATCH_UP_POLL_INTERVAL_SECONDS: Final[float] = 60.0
GIT_TIMEOUT_SECONDS: Final[float] = 600.0
# A completed git command slower than this is reported, so a mirror clone or
# push that is degrading shows up in the workflow log before it hits the hard timeout.
GIT_SLOW_WARNING_SECONDS: Final[float] = 60.0
# The userinfo part of a URL (``https://x-access-token:<token>@host/...``).
_URL_CREDENTIALS_PATTERN: Final[re.Pattern[str]] = re.compile(r"://[^/@\s]+@")


class MirrorTagError(Exception):
    """Base error for the tag re-pointing script."""


class GitCommandError(MirrorTagError):
    """Raised when a git command exits non-zero."""


class TagNotOnMirroredBranchError(MirrorTagError):
    """Raised when the tagged commit is not reachable from the exported branch."""


class MirrorNotCaughtUpError(MirrorTagError):
    """Raised when the mirror has not yet exported the tagged commit's position on main."""


class PublicTipNotExportedError(MirrorTagError):
    """Raised when the public branch tip carries no origin trailer, i.e. it was not produced by the sync."""


class NoPublicCommitError(MirrorTagError):
    """Raised when no first-parent ancestor of the tagged commit was ever exported."""


class PublicTagConflictError(MirrorTagError):
    """Raised when the public tag already exists on a different commit and --force was not given."""


class PublicTagPlan(FrozenModel):
    """Where a private tag lands on the public mirror."""

    tag_name: str = Field(description="The tag name, identical on both repos")
    private_sha: str = Field(description="The commit the private tag points at")
    public_sha: str = Field(description="The public commit the tag will point at")
    landing_private_sha: str = Field(
        description="The first-parent commit of the exported branch that carries the tag: the tagged commit itself, or the merge that landed it"
    )
    exported_private_sha: str = Field(
        description="The first-parent commit of the exported branch (possibly the landing commit itself) the public commit was exported from"
    )
    walked_commit_count: int = Field(
        description="First-parent commits of the exported branch inspected, starting at the landing commit, before a public counterpart was found"
    )
    message: str = Field(description="Annotated tag message, copied from the private tag")
    existing_public_sha: str | None = Field(
        description="The commit the public tag currently points at, or None when the mirror has no such tag"
    )

    @property
    def is_already_in_place(self) -> bool:
        return self.existing_public_sha == self.public_sha


@pure
def _redact_url_credentials(text: str) -> str:
    """Mask the userinfo of any URL in ``text`` so a push URL never prints its token."""
    return _URL_CREDENTIALS_PATTERN.sub("://***@", text)


def _run_git_process(
    repo: Path, *args: str, slow_warning_seconds: float = GIT_SLOW_WARNING_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``repo`` without prompting, returning the completed process for exit-code inspection."""
    started_at = time.monotonic()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        # Not chained: TimeoutExpired renders the full argv, push URL token included.
        command = _redact_url_credentials(" ".join(args))
        raise GitCommandError(f"git {command} timed out after {GIT_TIMEOUT_SECONDS:.0f}s") from None
    elapsed_seconds = time.monotonic() - started_at
    if elapsed_seconds > slow_warning_seconds:
        command = _redact_url_credentials(" ".join(args))
        print(
            f"warning: git {command} took {elapsed_seconds:.0f}s, over the {slow_warning_seconds:.0f}s expected",
            file=sys.stderr,
        )
    return result


@pure
def _git_failure(args: Sequence[str], result: subprocess.CompletedProcess[str]) -> GitCommandError:
    command = _redact_url_credentials(" ".join(args))
    stderr = _redact_url_credentials(result.stderr.strip())
    return GitCommandError(f"git {command} failed ({result.returncode}): {stderr}")


def run_git(repo: Path, *args: str) -> str:
    """Run a git command in ``repo`` and return its stripped stdout."""
    result = _run_git_process(repo, *args)
    if result.returncode != 0:
        raise _git_failure(args, result)
    return result.stdout.strip()


def _is_known_commit(repo: Path, sha: str) -> bool:
    args = ("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    result = _run_git_process(repo, *args)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise _git_failure(args, result)


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    args = ("merge-base", "--is-ancestor", ancestor, descendant)
    result = _run_git_process(repo, *args)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise _git_failure(args, result)


def resolve_tag_commit(repo: Path, tag_name: str) -> str:
    """The commit a tag points at, peeling an annotated tag object."""
    return run_git(repo, "rev-parse", "--verify", f"refs/tags/{tag_name}^{{commit}}")


def read_tag_message(repo: Path, tag_name: str) -> str:
    """The annotated tag's message; a lightweight tag yields the tag name itself.

    Subject and body only: a signed private tag's signature block is part of ``%(contents)``,
    and copying it into the re-created public tag would make that tag look signed (and fail
    to verify).
    """
    log_format = "%(objecttype)%0a%(contents:subject)%0a%0a%(contents:body)"
    output = run_git(repo, "for-each-ref", f"--format={log_format}", f"refs/tags/{tag_name}")
    object_type, _, contents = output.partition("\n")
    if object_type != "tag" or not contents.strip():
        return tag_name
    return contents.strip()


def first_parent_history(repo: Path, ref: str) -> list[str]:
    """The commit and its first-parent ancestors, newest first."""
    return run_git(repo, "rev-list", "--first-parent", ref).splitlines()


@pure
def landing_commit_index(first_parent_shas: Sequence[str], is_containing_tag: Callable[[str], bool]) -> int:
    """Index in a newest-first first-parent chain of the oldest commit that contains the tagged commit.

    That is the tagged commit itself when it sits on the chain, otherwise the merge that landed it.
    Containment is monotonic along the chain (once a commit contains the tag, every newer one does),
    so a binary search needs only log2(n) ancestry checks. The chain's tip must contain the tag.
    """
    assert first_parent_shas and is_containing_tag(first_parent_shas[0]), "the chain tip must contain the tag"
    oldest_known_containing = 0
    oldest_possible = len(first_parent_shas) - 1
    while oldest_known_containing < oldest_possible:
        middle = (oldest_known_containing + oldest_possible + 1) // 2
        if is_containing_tag(first_parent_shas[middle]):
            oldest_known_containing = middle
        else:
            oldest_possible = middle - 1
    return oldest_known_containing


def public_sha_by_exported_private_sha(public_repo: Path, branch: str) -> dict[str, str]:
    """Map each private SHA named in a ``GitOrigin-RevId`` trailer to the public commit carrying it.

    Public commits without the trailer (the pre-cutover seed history) are skipped.
    """
    log_format = f"%H%x09%(trailers:key={ORIGIN_TRAILER_KEY},valueonly,separator=%x2C)"
    mapping: dict[str, str] = {}
    for line in run_git(public_repo, "log", f"--format={log_format}", branch).splitlines():
        public_sha, _, trailer_values = line.partition("\t")
        for private_sha in trailer_values.split(","):
            stripped = private_sha.strip()
            if stripped and stripped not in mapping:
                mapping[stripped] = public_sha
    return mapping


def newest_exported_private_sha(public_repo: Path, branch: str) -> str | None:
    """The private SHA the newest public commit on ``branch`` was exported from, or None if it has no trailer."""
    log_format = f"%(trailers:key={ORIGIN_TRAILER_KEY},valueonly)"
    value = run_git(public_repo, "log", "-1", f"--format={log_format}", branch)
    return value.splitlines()[0].strip() if value else None


def public_branch_tip_sha(public_repo: Path, branch: str) -> str:
    return run_git(public_repo, "rev-parse", "--verify", f"{branch}^{{commit}}")


class ExportedAncestor(FrozenModel):
    """The newest first-parent commit at or before a tag's landing commit that the mirror exported."""

    private_sha: str = Field(description="The private commit that was exported")
    public_sha: str = Field(description="The public commit it was exported as")
    walked_commit_count: int = Field(description="How many first-parent commits were walked to reach it, from 1")


@pure
def find_exported_ancestor(
    first_parent_shas: Sequence[str],
    public_by_private: Mapping[str, str],
) -> ExportedAncestor | None:
    """The first (newest) first-parent SHA with a public counterpart, or None when none was exported."""
    for walked_commit_count, private_sha in enumerate(first_parent_shas, start=1):
        public_sha = public_by_private.get(private_sha)
        if public_sha is not None:
            return ExportedAncestor(
                private_sha=private_sha, public_sha=public_sha, walked_commit_count=walked_commit_count
            )
    return None


def existing_public_tag_sha(public_repo: Path, tag_name: str) -> str | None:
    args = ("rev-parse", "--verify", "--quiet", f"refs/tags/{tag_name}^{{commit}}")
    result = _run_git_process(public_repo, *args)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    raise _git_failure(args, result)


def is_mirror_caught_up_to(private_repo: Path, private_sha: str, public_repo: Path) -> bool:
    """Whether the mirror has exported at least up to ``private_sha``'s position on the exported branch.

    Judged from the newest exported private SHA alone, so a run of private-only commits at the tip
    of the branch reads as the mirror lagging until something after them is exported. A newest
    exported SHA the private checkout does not know was exported from a commit that landed on the
    branch after the checkout was taken, so it is past everything the checkout can see, the tagged
    commit included.

    Raises ``PublicTipNotExportedError`` when the public tip did not come from the sync: waiting
    cannot fix that, so it must not be mistaken for the mirror merely lagging.
    """
    newest = newest_exported_private_sha(public_repo, MIRRORED_BRANCH)
    if newest is None:
        tip = public_branch_tip_sha(public_repo, MIRRORED_BRANCH)
        raise PublicTipNotExportedError(
            f"the public {MIRRORED_BRANCH} tip ({tip[:12]}) carries no {ORIGIN_TRAILER_KEY} trailer, so it was "
            f"not produced by the mirror push; repair the mirror before re-pointing tags."
        )
    if not _is_known_commit(private_repo, newest):
        print(
            f"the mirror's newest exported commit ({newest[:12]}) is not in the private checkout, "
            f"so it landed on {MIRRORED_BRANCH} after the checkout: the mirror is past {private_sha[:12]}"
        )
        return True
    return _is_ancestor(private_repo, private_sha, newest)


def clone_public_mirror(public_url: str, destination: Path) -> None:
    """Bare, blob-less clone: only commits and refs are needed to map and push tags."""
    run_git(destination.parent, "clone", "--quiet", "--bare", "--filter=blob:none", public_url, str(destination))


def refresh_public_mirror(public_repo: Path, public_url: str) -> None:
    run_git(
        public_repo,
        "fetch",
        "--quiet",
        "--force",
        public_url,
        f"+refs/heads/{MIRRORED_BRANCH}:refs/heads/{MIRRORED_BRANCH}",
        "+refs/tags/*:refs/tags/*",
    )


def plan_public_tag(
    private_repo: Path,
    public_repo: Path,
    tag_name: str,
    mirrored_ref: str,
) -> PublicTagPlan:
    """Map a private tag to its public commit. Raises when the tag cannot be mirrored (yet)."""
    private_sha = resolve_tag_commit(private_repo, tag_name)
    if not _is_ancestor(private_repo, private_sha, mirrored_ref):
        raise TagNotOnMirroredBranchError(
            f"{tag_name} ({private_sha[:12]}) is not reachable from {mirrored_ref}; the mirror exports "
            f"only {MIRRORED_BRANCH}, so no public commit carries this tag's tree."
        )
    if not is_mirror_caught_up_to(private_repo, private_sha, public_repo):
        raise MirrorNotCaughtUpError(
            f"the mirror has not yet exported {tag_name} ({private_sha[:12]}); its newest exported commit "
            f"is older than the tagged commit."
        )
    mirrored_history = first_parent_history(private_repo, mirrored_ref)
    landing_index = landing_commit_index(
        mirrored_history, lambda chain_sha: _is_ancestor(private_repo, private_sha, chain_sha)
    )
    landing_private_sha = mirrored_history[landing_index]
    found = find_exported_ancestor(
        mirrored_history[landing_index:],
        public_sha_by_exported_private_sha(public_repo, MIRRORED_BRANCH),
    )
    if found is None:
        raise NoPublicCommitError(
            f"no first-parent ancestor of {landing_private_sha[:12]} on {mirrored_ref}, which carries {tag_name} "
            f"({private_sha[:12]}), was ever exported"
        )
    return PublicTagPlan(
        tag_name=tag_name,
        private_sha=private_sha,
        public_sha=found.public_sha,
        landing_private_sha=landing_private_sha,
        exported_private_sha=found.private_sha,
        walked_commit_count=found.walked_commit_count,
        message=read_tag_message(private_repo, tag_name),
        existing_public_sha=existing_public_tag_sha(public_repo, tag_name),
    )


def wait_for_public_tag_plan(
    private_repo: Path,
    public_repo: Path,
    public_url: str,
    tag_name: str,
    mirrored_ref: str,
    deadline: float,
    poll_interval_seconds: float,
) -> PublicTagPlan:
    """Plan the tag, re-fetching the mirror and retrying while it has not caught up, until the monotonic ``deadline``.

    The deadline is shared by every tag of a run: once the mirror is behind one tag it is behind
    every newer one, so a backfill must not wait the full budget again per tag.
    """

    def _is_past_deadline(retry_state: RetryCallState) -> bool:
        return time.monotonic() >= deadline

    def _refresh_before_retry(retry_state: RetryCallState) -> None:
        if retry_state.attempt_number > 1:
            refresh_public_mirror(public_repo, public_url)

    def _report_retry(retry_state: RetryCallState) -> None:
        print(f"{tag_name}: mirror not caught up yet; retrying in {poll_interval_seconds:.0f}s")

    retrying = Retrying(
        retry=retry_if_exception_type(MirrorNotCaughtUpError),
        stop=_is_past_deadline,
        wait=wait_fixed(poll_interval_seconds),
        before=_refresh_before_retry,
        before_sleep=_report_retry,
        reraise=True,
    )
    return retrying(plan_public_tag, private_repo, public_repo, tag_name, mirrored_ref)


def push_public_tag(public_repo: Path, plan: PublicTagPlan, push_url: str, is_force: bool) -> None:
    """Create the annotated tag in the public clone and push it. Raises on an unforced conflict."""
    if plan.existing_public_sha is not None and not plan.is_already_in_place and not is_force:
        raise PublicTagConflictError(
            f"{plan.tag_name} already exists on the mirror at {plan.existing_public_sha[:12]}, "
            f"not {plan.public_sha[:12]}; pass --force to re-point it."
        )
    run_git(
        public_repo,
        "-c",
        f"user.name={TAG_COMMITTER_NAME}",
        "-c",
        f"user.email={TAG_COMMITTER_EMAIL}",
        "tag",
        "--annotate",
        "--force",
        "--message",
        plan.message,
        plan.tag_name,
        plan.public_sha,
    )
    push_args = ["push", "--quiet", *(["--force"] if is_force else []), push_url, f"refs/tags/{plan.tag_name}"]
    run_git(public_repo, *push_args)


def list_tags(repo: Path, glob: str) -> list[str]:
    return run_git(repo, "tag", "--list", glob).splitlines()


@pure
def tags_missing_from_mirror(private_tags: Sequence[str], public_tags: Sequence[str], glob: str) -> list[str]:
    """Private tags matching ``glob`` that the mirror does not have, in version-sorted order."""
    public = set(public_tags)
    missing = [tag for tag in private_tags if fnmatch.fnmatch(tag, glob) and tag not in public]
    return sorted(missing, key=_version_sort_key)


# Natural sort: digit runs compare numerically, everything else as text, and the
# (0|1) marker keeps int and str from ever meeting in a comparison.
_TAG_NAME_RUN_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d+|\D+")


@pure
def _version_sort_key(tag_name: str) -> tuple[tuple[int, int | str], ...]:
    return tuple((0, int(run)) if run.isdigit() else (1, run) for run in _TAG_NAME_RUN_PATTERN.findall(tag_name))


@pure
def _describe_plan(plan: PublicTagPlan) -> str:
    if plan.is_already_in_place:
        state = "already on the mirror"
    elif plan.existing_public_sha is None:
        state = "new on the mirror"
    else:
        state = f"CONFLICT: mirror has it at {plan.existing_public_sha[:12]}"
    landed = (
        ""
        if plan.landing_private_sha == plan.private_sha
        else f" landed on {MIRRORED_BRANCH} by merge {plan.landing_private_sha[:12]}"
    )
    via = (
        ""
        if plan.exported_private_sha == plan.landing_private_sha
        else f" via ancestor {plan.exported_private_sha[:12]}"
    )
    return (
        f"{plan.tag_name}: private {plan.private_sha[:12]}{landed} -> public {plan.public_sha[:12]}"
        f"{via} ({plan.walked_commit_count} first-parent commit(s) walked); {state}"
    )


class MirrorTagArguments(FrozenModel):
    """Parsed command line for the script."""

    tag_names: tuple[str, ...] = Field(description="Explicit tags to re-point; empty with --all-missing")
    is_all_missing: bool = Field(description="Re-point every private tag matching the glob that the mirror lacks")
    private_repo: Path = Field(description="Path to the private checkout with full history")
    public_url: str = Field(description="Public mirror clone URL (read side)")
    push_url: SecretStr | None = Field(description="Authenticated push URL (carries the token); None in --dry-run")
    mirrored_ref: str = Field(description="Ref in the private repo that names the exported branch tip")
    is_force: bool = Field(description="Re-point a tag the mirror already has elsewhere")
    is_dry_run: bool = Field(description="Plan and report only; push nothing")
    catch_up_timeout_seconds: float = Field(
        description="How long to wait for the mirror push to export the tagged commit"
    )


def _parse_arguments(argv: Sequence[str]) -> MirrorTagArguments:
    parser = argparse.ArgumentParser(description="Re-point private minds-v* tags onto the public mirror.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--tag", action="append", default=[], help="Tag to re-point (repeatable)")
    selection.add_argument(
        "--all-missing",
        action="store_true",
        help=f"Re-point every private {MIRRORED_TAG_GLOB} tag the mirror does not have",
    )
    parser.add_argument(
        "--private-repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Private checkout with full history (default: this repo)",
    )
    parser.add_argument("--public-url", default=PUBLIC_REPO_URL, help="Public mirror URL to read from")
    parser.add_argument(
        "--push-url",
        default=None,
        help=f"Push URL; defaults to the public URL with the {PUSH_TOKEN_ENV_VAR} token when that env var is set",
    )
    parser.add_argument(
        "--mirrored-ref",
        default=DEFAULT_MIRRORED_REF,
        help=f"Private ref naming the exported branch tip (default {DEFAULT_MIRRORED_REF})",
    )
    parser.add_argument("--force", action="store_true", help="Re-point a tag that already exists elsewhere")
    parser.add_argument("--dry-run", action="store_true", help="Plan and report only; push nothing")
    parser.add_argument(
        "--catch-up-timeout-seconds",
        type=float,
        default=DEFAULT_CATCH_UP_TIMEOUT_SECONDS,
        help="How long to wait for the mirror push to export the tagged commit",
    )
    namespace = parser.parse_args(argv)
    push_url = _resolve_push_url(namespace.push_url)
    if push_url is None and not namespace.dry_run:
        parser.error(f"set {PUSH_TOKEN_ENV_VAR} (or pass --push-url), or use --dry-run")
    return MirrorTagArguments(
        tag_names=tuple(namespace.tag),
        is_all_missing=namespace.all_missing,
        private_repo=namespace.private_repo,
        public_url=namespace.public_url,
        push_url=push_url,
        mirrored_ref=namespace.mirrored_ref,
        is_force=namespace.force,
        is_dry_run=namespace.dry_run,
        catch_up_timeout_seconds=namespace.catch_up_timeout_seconds,
    )


def _resolve_push_url(explicit_push_url: str | None) -> SecretStr | None:
    if explicit_push_url:
        return SecretStr(explicit_push_url)
    token = os.environ.get(PUSH_TOKEN_ENV_VAR)
    if token:
        return SecretStr(PUBLIC_PUSH_URL_TEMPLATE.format(token=token))
    return None


def _select_tags(arguments: MirrorTagArguments, public_repo: Path) -> list[str]:
    if not arguments.is_all_missing:
        return list(arguments.tag_names)
    missing = tags_missing_from_mirror(
        list_tags(arguments.private_repo, MIRRORED_TAG_GLOB),
        list_tags(public_repo, MIRRORED_TAG_GLOB),
        MIRRORED_TAG_GLOB,
    )
    print(f"{len(missing)} {MIRRORED_TAG_GLOB} tag(s) missing from the mirror: {', '.join(missing) or '(none)'}")
    return missing


def _repoint_tags(arguments: MirrorTagArguments, public_repo: Path) -> int:
    """Plan (and unless dry-run, push) every selected tag; returns the count that could not be mirrored."""
    failure_count = 0
    catch_up_deadline = time.monotonic() + arguments.catch_up_timeout_seconds
    for tag_name in _select_tags(arguments, public_repo):
        try:
            plan = wait_for_public_tag_plan(
                arguments.private_repo,
                public_repo,
                arguments.public_url,
                tag_name,
                arguments.mirrored_ref,
                catch_up_deadline,
                poll_interval_seconds=CATCH_UP_POLL_INTERVAL_SECONDS,
            )
        except TagNotOnMirroredBranchError as e:
            # Under --all-missing a branch-only tag (a test tag) is expected and merely reported;
            # asked for explicitly, it is an error.
            if arguments.is_all_missing:
                print(f"{tag_name}: skipped -- {e}")
                continue
            print(f"{tag_name}: error -- {e}", file=sys.stderr)
            failure_count += 1
            continue
        except MirrorTagError as e:
            print(f"{tag_name}: error -- {e}", file=sys.stderr)
            failure_count += 1
            continue
        print(_describe_plan(plan))
        if arguments.is_dry_run or plan.is_already_in_place:
            continue
        if arguments.push_url is None:
            raise MirrorTagError("no push URL configured for a non-dry run")
        try:
            push_public_tag(public_repo, plan, arguments.push_url.get_secret_value(), arguments.is_force)
        except MirrorTagError as e:
            print(f"{tag_name}: error -- {e}", file=sys.stderr)
            failure_count += 1
            continue
        print(f"{tag_name}: pushed to the mirror at {plan.public_sha[:12]}")
    return failure_count


def main(argv: Sequence[str]) -> int:
    arguments = _parse_arguments(argv)
    with tempfile.TemporaryDirectory(prefix="mirror-tag-") as temp_dir:
        public_repo = Path(temp_dir) / "public.git"
        print(f"Cloning the public mirror ({arguments.public_url}) ...")
        clone_public_mirror(arguments.public_url, public_repo)
        failure_count = _repoint_tags(arguments, public_repo)
    if failure_count:
        print(f"{failure_count} tag(s) could not be mirrored", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
