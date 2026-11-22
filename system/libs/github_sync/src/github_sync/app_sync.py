"""The app pass: keeping each user app's private dev repository in step with
the app's directory.

For every app that has a dev repository the pass compares the tree of the
app's committed directory with the tree it last pushed, and when they differ
splits the app's own history out of the workspace history and pushes it to
the dev repository's `main`, fast-forward only. It never commits, never moves
a ref of the workspace repository, and never forces a push.

What was last pushed is machine state, one file per app under
`data/.state/app_sync/`. The file only saves work: without it the pass splits
and pushes again, a split reproduces the same commits, and the push changes
nothing the dev repository already has.
"""

import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from enum import auto
from pathlib import Path

from app_manifest.errors import AppManifestError
from app_manifest.records import (
    RECORDS_DIRECTORY,
    AppOrigin,
    AppRecord,
    parse_record,
)
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from loguru import logger
from pydantic import Field

from github_sync.app_history import (
    AppHistoryError,
    app_tree,
    is_app_linked,
    is_app_modified,
    is_primary_checkout,
    run_git,
    split_app,
)
from github_sync.config import GithubSyncError
from github_sync.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNKNOWN,
    is_visibility_check_due,
)
from github_sync.wiring import git_environment_through_gateway

# Relative to the workspace repo's root, like every other workspace path.
APP_SYNC_STATE_DIRECTORY = Path("data/.state/app_sync")
DEV_REPOSITORY_BRANCH = "main"
# What git says of a push the remote branch is not an ancestor of.
_NON_FAST_FORWARD_MARKERS = ("(non-fast-forward)", "(fetch first)")
# What a split says when it had to leave the commits made since the app's
# newest marker: the shell's refusal to go deeper, or the time it is given.
_SPLIT_LEFT_ITS_MARKER_SIGNS = ("recursion depth", "timed out")


class AppSyncError(GithubSyncError):
    """The app pass could not run at all."""


class AppSyncState(LowerCaseStrEnum):
    """What the pass found, or did, for one app."""

    # The app's history was split and pushed to its dev repository.
    PUSHED = auto()
    # The app's directory is what was last pushed.
    UP_TO_DATE = auto()
    # An installed app nobody has changed: it has no dev repository.
    UNMODIFIED = auto()
    # Skipped: the app's record names no dev repository yet.
    NO_DEV_REPOSITORY = auto()
    # Skipped: an app this workspace built whose first split has not been made.
    NOT_LINKED = auto()
    # Held: the dev repository is public.
    NOT_PRIVATE = auto()
    # Held: the dev repository has not been confirmed private yet.
    VISIBILITY_UNKNOWN = auto()
    # The dev repository holds history this workspace's split does not continue.
    PUSH_REFUSED = auto()
    # Anything else that stopped this app.
    FAILED = auto()


SYNCED_STATES = frozenset({AppSyncState.PUSHED, AppSyncState.UP_TO_DATE})


class PushedState(FrozenModel):
    """What the pass last pushed for one app; the content of its state file."""

    dev_repo_url: str = Field(description="The dev repository that was pushed to")
    pushed_commit: str = Field(description="The commit of the app's own history")
    pushed_tree: str = Field(description="That commit's tree, the app's directory")


class AppSyncReport(FrozenModel):
    """One app's line in the service's status."""

    name: str = Field(description="The app")
    state: AppSyncState = Field(description="What the pass found or did")
    dev_repo_url: str = Field(default="", description="The app's dev repository")
    visibility: str = Field(
        default=VISIBILITY_UNKNOWN,
        description="The dev repository's last confirmed visibility",
    )
    pushed_commit: str = Field(
        default="", description="The commit the dev repository was last brought to"
    )
    detail: str = Field(default="", description="Why, and what to do about it")


class VisibilityAnswer(FrozenModel):
    """One repository's visibility, and when it was confirmed."""

    visibility: str = Field(description="private or public")
    checked_at: datetime = Field(description="When GitHub last gave that answer")


class VisibilityLedger(MutableModel):
    """The last confirmed visibility of each dev repository, kept across ticks
    so each is asked about once per check interval rather than once per tick."""

    answer_by_url: dict[str, VisibilityAnswer] = Field(
        default_factory=dict, description="The confirmed answer for each repository URL"
    )

    def refresh(
        self, repo_url: str, check_visibility: Callable[[str], str], now: datetime
    ) -> str:
        """The repository's visibility, asked again when the confirmed answer
        has gone stale. A check that fails outright keeps the last confirmed
        answer, as the workspace's own sync repository does."""
        previous = self.answer_by_url.get(repo_url)
        if previous is not None and not is_visibility_check_due(
            previous.visibility, previous.checked_at, now
        ):
            return previous.visibility
        visibility = check_visibility(repo_url)
        if visibility == VISIBILITY_UNKNOWN:
            return previous.visibility if previous is not None else VISIBILITY_UNKNOWN
        self.answer_by_url[repo_url] = VisibilityAnswer(
            visibility=visibility, checked_at=now
        )
        return visibility


def pushed_state_path(repo_root: Path, name: str) -> Path:
    """Where the pass keeps what it last pushed for the app `name`."""
    return repo_root / APP_SYNC_STATE_DIRECTORY / f"{name}.json"


def load_pushed_state(repo_root: Path, name: str) -> PushedState | None:
    """What was last pushed for the app, or None when nothing is recorded or
    the record cannot be read: the pass then splits and pushes again."""
    path = pushed_state_path(repo_root, name)
    try:
        return PushedState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Ignoring the unreadable app sync state {}: {}", path, e)
        return None


def _write_pushed_state(repo_root: Path, name: str, state: PushedState) -> None:
    path = pushed_state_path(repo_root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = path.with_name(path.name + ".tmp")
    incoming.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(incoming, path)


def _push_split(repo_root: Path, dev_repo_url: str, commit: str) -> None:
    """Push `commit` to the dev repository's branch as a fast-forward, through
    the latchkey gateway when the workspace has one. Never forced."""
    environment = git_environment_through_gateway(os.environ)
    run_git(
        repo_root,
        [
            "push",
            "--quiet",
            dev_repo_url,
            f"{commit}:refs/heads/{DEV_REPOSITORY_BRANCH}",
        ],
        environment,
    )


def _reported(report: AppSyncReport, state: AppSyncState, detail: str) -> AppSyncReport:
    return report.model_copy_update(
        to_update(report.field_ref().state, state),
        to_update(report.field_ref().detail, detail),
    )


def _sync_app(
    repo_root: Path,
    record: AppRecord,
    ledger: VisibilityLedger,
    check_visibility: Callable[[str], str],
) -> AppSyncReport:
    name = str(record.name)
    is_owned = record.origin is AppOrigin.LOCAL
    if not record.dev_repo_url:
        if is_owned or is_app_modified(repo_root, record):
            return AppSyncReport(
                name=name,
                state=AppSyncState.NO_DEV_REPOSITORY,
                detail=f"skipped: the app has no dev repository; `app-install link {name}` makes one",
            )
        return AppSyncReport(name=name, state=AppSyncState.UNMODIFIED)

    visibility = ledger.refresh(
        record.dev_repo_url, check_visibility, datetime.now(timezone.utc)
    )
    pushed = load_pushed_state(repo_root, name)
    if pushed is not None and pushed.dev_repo_url != record.dev_repo_url:
        # What was pushed to a repository the app no longer names says nothing of this one.
        pushed = None
    unsynced = AppSyncReport(
        name=name,
        state=AppSyncState.FAILED,
        dev_repo_url=record.dev_repo_url,
        visibility=visibility,
        pushed_commit=pushed.pushed_commit if pushed is not None else "",
    )
    if visibility == VISIBILITY_PUBLIC:
        return _reported(
            unsynced,
            AppSyncState.NOT_PRIVATE,
            "held: the dev repository is public; make it private again to resume",
        )
    if visibility != VISIBILITY_PRIVATE:
        return _reported(
            unsynced,
            AppSyncState.VISIBILITY_UNKNOWN,
            "held: the dev repository has not been confirmed private yet",
        )
    tree = app_tree(repo_root, record)
    if tree is None:
        return _reported(
            unsynced, AppSyncState.FAILED, "no commit holds the app's directory yet"
        )
    if pushed is not None and pushed.pushed_tree == tree:
        return _reported(unsynced, AppSyncState.UP_TO_DATE, "")
    if is_owned and not is_app_linked(repo_root, record):
        return _reported(
            unsynced,
            AppSyncState.NOT_LINKED,
            f"skipped: the app's first split has not been made; run `app-install link {name}`",
        )

    try:
        commit = split_app(repo_root, record)
    except AppHistoryError as e:
        if is_owned and any(sign in str(e) for sign in _SPLIT_LEFT_ITS_MARKER_SIGNS):
            return _reported(
                unsynced,
                AppSyncState.FAILED,
                "nothing was pushed: the app's history could not be read from its newest marker on, as "
                "happens once a merge brings in commits older than the marker (an update of the workspace "
                f"does); run `app-install link {name} --refresh`, which makes a marker past the merge ({e})",
            )
        raise
    try:
        _push_split(repo_root, record.dev_repo_url, commit)
    except AppHistoryError as e:
        if any(marker in str(e) for marker in _NON_FAST_FORWARD_MARKERS):
            return _reported(
                unsynced,
                AppSyncState.PUSH_REFUSED,
                "nothing was pushed: the dev repository holds history this workspace's copy of the app does "
                f"not continue, as when another workspace pushes to it too ({e})",
            )
        raise
    _write_pushed_state(
        repo_root,
        name,
        PushedState(
            dev_repo_url=record.dev_repo_url,
            pushed_commit=commit,
            pushed_tree=run_git(repo_root, ["rev-parse", f"{commit}^{{tree}}"]),
        ),
    )
    return _reported(unsynced, AppSyncState.PUSHED, "").model_copy_update(
        to_update(unsynced.field_ref().pushed_commit, commit)
    )


def sync_app(
    repo_root: Path,
    record: AppRecord,
    ledger: VisibilityLedger,
    check_visibility: Callable[[str], str],
) -> AppSyncReport:
    """Bring one app's dev repository in step with its directory, and say what
    happened. A failure is reported, never raised: one app does not stop the
    next, or the workspace's own sync."""
    try:
        return _sync_app(repo_root, record, ledger, check_visibility)
    except (GithubSyncError, OSError) as e:
        logger.debug("The app pass failed for {}: {}", record.name, e)
        return AppSyncReport(
            name=str(record.name),
            state=AppSyncState.FAILED,
            dev_repo_url=record.dev_repo_url,
            detail=str(e),
        )


def require_primary_checkout(repo_root: Path) -> None:
    """Raise AppSyncError unless `repo_root` is a repository's primary checkout:
    a linked worktree is on a branch of its own, whose history is not the app's."""
    try:
        is_primary = is_primary_checkout(repo_root)
    except AppHistoryError as e:
        raise AppSyncError(f"{repo_root} is not a git checkout: {e}") from e
    if not is_primary:
        raise AppSyncError(
            f"{repo_root} is not the root of the workspace's primary checkout, the only place apps are "
            "synced from (a linked worktree is on a branch of its own)"
        )


def run_app_pass(
    repo_root: Path,
    ledger: VisibilityLedger,
    check_visibility: Callable[[str], str],
) -> tuple[AppSyncReport, ...]:
    """Run the pass over every user app of the workspace at `repo_root`, by name.

    Raises AppSyncError when `repo_root` is not the primary checkout. A record
    that cannot be read is reported under its file's name and skipped.
    """
    require_primary_checkout(repo_root)
    reports: list[AppSyncReport] = []
    for path in sorted((repo_root / RECORDS_DIRECTORY).glob("*.toml")):
        if not path.is_file():
            continue
        try:
            record = parse_record(path.read_text(encoding="utf-8"), path)
        except (AppManifestError, OSError) as e:
            reports.append(
                AppSyncReport(name=path.stem, state=AppSyncState.FAILED, detail=str(e))
            )
            continue
        reports.append(sync_app(repo_root, record, ledger, check_visibility))
    return tuple(reports)
