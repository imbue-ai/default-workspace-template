"""Which ref to update to: the latest stable ``minds-v*`` tag not newer than the
Minds app driving the workspace (the ceiling), or an explicit override.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple
from typing import Sequence

# A released minds version tag, e.g. ``minds-v0.3.7`` (stable) or
# ``minds-v0.3.7-rc1`` (a release candidate -- a prerelease we never default to).
_TAG_RE = re.compile(r"^minds-v(\d+)\.(\d+)\.(\d+)(?:-(?P<pre>.+))?$")


class NoUpdateTargetError(ValueError):
    """Raised when no ref to update to could be chosen.

    A refusal, not a fault: the workspace is fine, there is simply nothing it may
    update to right now. Distinct from a plain ``ValueError`` so the CLI can render
    this case as a one-line explanation and let a genuine bug keep its traceback.
    """


class ResolvedTarget(NamedTuple):
    """The ref the update merges in, plus a coarse ``kind`` for the caller's log.

    ``kind`` is ``tag`` (a resolved ``minds-v*`` release), ``branch`` (``main``),
    or ``ref`` (any other override passed straight through for git to validate).

    ``ceiling`` is the template ref the app reported, passed through as given --
    it caps only insofar as it parses as a release, so a dev build's branch name
    is carried here and caps nothing. ``None`` means no ceiling was supplied at
    all, which only a direct caller does. ``exceeds_ceiling`` marks an override the
    ceiling could not vouch for: newer than the app, or a branch/commit carrying no
    version to compare; the default (no-override) path never sets it.
    """

    ref: str
    kind: str
    ceiling: str | None = None
    exceeds_ceiling: bool = False


class Version(NamedTuple):
    """A ``minds-v*`` tag's version, ordered by plain ``<`` the way semver orders.

    **Field order is the precedence order** -- comparison is tuple comparison, so
    reordering these silently changes which release outranks which.

    ``release_rank`` is 0 for a prerelease and 1 for the release it precedes, so
    ``0.4.0-rc1 < 0.4.0``; ``prerelease`` then breaks ties among prereleases of
    the same version. It has to be a *field* rather than a property derived from an
    empty ``prerelease``: only a field participates in the comparison.
    """

    major: int
    minor: int
    patch: int
    release_rank: int
    prerelease: tuple[tuple[int, int, str], ...]

    @property
    def is_stable(self) -> bool:
        """Whether this is a released version rather than a prerelease of one."""
        return not self.prerelease


def _prerelease_sort_key(pre: str) -> tuple[tuple[int, int, str], ...]:
    """Order a prerelease's dot-separated identifiers the way semver does.

    Numeric identifiers compare numerically and rank below alphanumeric ones, so
    ``rc.2`` follows ``rc.1`` rather than sorting lexically (where ``rc.10`` would
    land before ``rc.2``). Each identifier becomes ``(is_alphanumeric, number,
    text)`` so a single tuple comparison covers both kinds.

    "Numeric" is ``isdecimal``, semver's ``[0-9]+``, and not ``isdigit``, which
    also admits superscripts and other digits ``int()`` refuses to convert.
    """
    identifiers: list[tuple[int, int, str]] = []
    for identifier in pre.split("."):
        if identifier.isdecimal():
            identifiers.append((0, int(identifier), ""))
        else:
            identifiers.append((1, 0, identifier))
    return tuple(identifiers)


def parse_version(tag: str) -> Version | None:
    """Return the :class:`Version` of any ``minds-v*`` tag, prerelease included.

    Prereleases parse because a *ceiling* is a different question from a
    *candidate*: an app on ``minds-v0.4.0-rc1`` has a real version and should cap
    its workspaces. Candidate selection asks the separate question via
    :attr:`Version.is_stable`, so a prerelease still never wins the default
    "latest stable" pick.

    Ordering follows semver: a prerelease sorts below its own release, so a ceiling
    of ``minds-v0.4.0-rc1`` admits ``minds-v0.3.9`` but not ``minds-v0.4.0``.

    Returns ``None`` only for something that is not a release tag at all (a
    branch name, a bare commit) -- there is genuinely no version to compare.
    """
    match = _TAG_RE.match(tag.strip())
    if match is None:
        return None
    pre = match.group("pre")
    return Version(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        release_rank=0 if pre is not None else 1,
        prerelease=_prerelease_sort_key(pre) if pre is not None else (),
    )


def pick_latest_stable_tag(
    tags: Sequence[str], ceiling: str | None = None
) -> str | None:
    """Return the highest-versioned stable ``minds-v*`` tag, or ``None`` if none.

    Prereleases (``minds-v*-rc*``) and non-matching tags are ignored. Selection is
    by semantic version, not lexical order, so ``minds-v0.3.10`` beats
    ``minds-v0.3.9``.

    ``ceiling`` bounds the selection to tags at or below it, so a workspace never
    picks a template newer than the app driving it. It is parsed with
    :func:`parse_version`, so an app on a *prerelease* caps just as well as one on
    a stable release; only a ceiling that is not a release tag at all (a dev app
    reporting a branch) means no ceiling.

    Candidates are still filtered to *stable* tags: capping by a prerelease does
    not make one selectable.
    """
    ceiling_version = parse_version(ceiling) if ceiling is not None else None
    stable = [
        (version, tag)
        for tag in tags
        if (version := parse_version(tag)) is not None
        and version.is_stable
        and (ceiling_version is None or version <= ceiling_version)
    ]
    if not stable:
        return None
    return max(stable, key=lambda item: item[0])[1]


def is_held_back_by_ceiling(
    *,
    resolved_ref: str,
    latest_available: str | None,
    ceiling: str | None,
    has_override: bool,
) -> bool:
    """Whether the ceiling -- and not the user -- is why a newer release was not taken.

    Only true when the flow chose the target itself. With an explicit override the
    user picked the ref, so a gap between it and ``latest_available`` is their own
    doing; reporting "your app held this back" there blames the app for the user's
    choice (an ``--override`` to an *older* tag would otherwise trip it every time).
    """
    if has_override or ceiling is None or latest_available is None:
        return False
    return latest_available != resolved_ref


def _is_within_ceiling(ref: str, ceiling: str | None) -> bool:
    """Whether ``ref`` is provably a release at or below ``ceiling``.

    Both sides go through :func:`parse_version`, so a prerelease on either side
    compares properly rather than being written off. False for something with no
    version at all -- a branch or a bare commit -- where the ceiling genuinely
    cannot vouch for the ref. True when there is no ceiling to enforce.
    """
    ceiling_version = parse_version(ceiling) if ceiling is not None else None
    if ceiling_version is None:
        return True
    ref_version = parse_version(ref)
    return ref_version is not None and ref_version <= ceiling_version


def resolve_target(
    override: str | None,
    tags: Sequence[str],
    remote: str = "upstream",
    ceiling: str | None = None,
) -> ResolvedTarget:
    """Resolve the update target ref.

    With no override, pick the latest stable ``minds-v*`` tag at or below
    ``ceiling`` (raising if the upstream exposes none). An override of ``main``
    selects the template's default branch, **remote-qualified** to
    ``<remote>/main`` -- a bare ``main`` would resolve to the *local* branch, which
    ``git fetch upstream`` never advances, so the pull would merge stale local
    code. A tag, by contrast, lands in the local tag namespace on fetch and
    resolves by its bare name, so a known-tag override is returned as-is. Any
    other override is passed through verbatim as a ``ref`` for git to validate at
    fetch time (so a user can pin an arbitrary commit or a ref they've already
    qualified themselves).

    An override is never silently blocked -- the user asked for it by name -- but
    one that is not provably at or below ``ceiling`` comes back with
    ``exceeds_ceiling`` set, which the skill turns into an explicit user
    confirmation before anything is merged.
    """
    if override is None:
        latest = pick_latest_stable_tag(tags, ceiling=ceiling)
        if latest is None:
            raise NoUpdateTargetError(_no_target_message(tags, ceiling))
        return ResolvedTarget(latest, "tag", ceiling, False)
    exceeds = not _is_within_ceiling(override, ceiling)
    if override == "main":
        return ResolvedTarget(f"{remote}/{override}", "branch", ceiling, exceeds)
    if override in set(tags):
        return ResolvedTarget(override, "tag", ceiling, exceeds)
    return ResolvedTarget(override, "ref", ceiling, exceeds)


def _no_target_message(tags: Sequence[str], ceiling: str | None) -> str:
    """Explain why no default target could be picked, distinguishing the two causes."""
    if ceiling is not None and pick_latest_stable_tag(tags) is not None:
        return (
            f"every stable minds-v* tag upstream is newer than this workspace's minds "
            f"app ({ceiling}); update the app first, or pass an explicit --override "
            f"to update past it anyway"
        )
    return (
        "no stable minds-v* tag found upstream; pass an explicit "
        "--override (a tag, 'main', or a ref) to update anyway"
    )


def already_current_message(
    ref: str, latest_available: str | None, ceiling: str | None, is_held_back: bool
) -> str:
    """Explain that the default target is already merged, naming the ceiling when it is why.

    The two cases read very differently to a user and need different next steps.
    Held back: a newer release exists and the app is the only thing standing
    between them and it, so the message has to say so -- updating the app is the
    action that unblocks them. Not held back: the workspace is simply current,
    and there is nothing to do.
    """
    if is_held_back:
        return (
            f"this workspace is already on {ref}, the newest release your minds app "
            f"({ceiling}) supports; {latest_available} is available upstream but needs a "
            f"newer app -- update the app first, or pass an explicit --override to update "
            f"past it anyway"
        )
    return f"this workspace is already on {ref}, the newest release upstream; nothing to update"


# The minds app's version route, addressed through the latchkey gateway's
# ``minds-api-proxy`` on the reserved gateway-self host. Allowed by the agent
# permissions baseline (``minds-app-version-read``), so this needs no grant and
# never raises a permission dialog -- which matters because update-self resolves
# its target from a background worker, with nobody watching to approve one.
_MINDS_APP_VERSION_URL = (
    "http://latchkey-self.invalid/minds-api-proxy/api/v1/app/version"
)

# Bounds the gateway round-trip, at the house network default (the style guide's
# 60s, matching this repo's other ``latchkey curl``, ``github_sync``'s
# ``_LATCHKEY_CURL_TIMEOUT_SECONDS``).
_APP_VERSION_TIMEOUT_SECONDS = 60

# Statuses that mean "this app predates the version route", not "something went
# wrong". 404 is the obvious one; 403 is in fact the *likelier* of the two, since
# the route and the gateway permission that reaches it (``minds-app-version-read``)
# ship in the same release, so an app old enough to lack the route also lacks the
# grant -- and the gateway denies an ungranted request before the app ever sees it.
_APP_TOO_OLD_STATUSES = frozenset({"403", "404"})


class CeilingUnavailableError(Exception):
    """Raised when the minds app's update ceiling could not be read.

    Never downgraded to "no ceiling": an app that cannot answer is very often an
    app too old to *have* this route, which is exactly the case the ceiling
    protects against.
    """


def fetch_app_template_ref(url: str = _MINDS_APP_VERSION_URL) -> str:
    """Return the newest workspace-template ref the running minds app supports.

    Goes through ``latchkey curl``, which injects the gateway credentials and
    passes every other argument (and curl's exit code) straight through. Each
    failure mode is reported distinctly, because the user's next action differs:
    a transport failure is worth retrying, an :data:`_APP_TOO_OLD_STATUSES`
    answer (403 or 404) means the app must be updated first, and any other bad
    status or malformed body is a bug worth reporting.
    """
    with tempfile.NamedTemporaryFile(suffix=".json") as body_file:
        try:
            result = subprocess.run(
                [
                    "latchkey",
                    "curl",
                    "--silent",
                    "--show-error",
                    "--output",
                    body_file.name,
                    "--write-out",
                    "%{http_code}",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=_APP_VERSION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CeilingUnavailableError(
                f"could not reach the minds app to read its version ({e}). The app may be "
                f"closed or the gateway down; retry once it is running."
            ) from e
        if result.returncode != 0:
            raise CeilingUnavailableError(
                f"could not reach the minds app to read its version (latchkey curl exited "
                f"{result.returncode}: {result.stderr.strip()}). The app may be closed or the "
                f"gateway down; retry once it is running."
            )
        status = result.stdout.strip()
        body = Path(body_file.name).read_text()

    if status in _APP_TOO_OLD_STATUSES:
        raise CeilingUnavailableError(
            "this workspace's minds app is too old to report its version (it answered "
            f"HTTP {status} for {url}), so there is no way to tell how far this workspace "
            "may safely update. Update the minds app itself first."
        )
    if status != "200":
        raise CeilingUnavailableError(
            f"the minds app returned HTTP {status} for its version ({body.strip()[:200]})."
        )
    try:
        template_ref = json.loads(body)["workspace_template_ref"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise CeilingUnavailableError(
            f"the minds app's version response could not be parsed ({e}): {body.strip()[:200]}"
        ) from e
    if not isinstance(template_ref, str) or not template_ref:
        raise CeilingUnavailableError(
            f"the minds app reported an empty workspace_template_ref: {body.strip()[:200]}"
        )
    return template_ref
