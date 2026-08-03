"""GitHub sync service loop.

The service is a wiring and visibility watchdog for the opt-in GitHub sync;
the actual pushing happens in the post-commit hook that the wiring activates.
Each tick re-applies the latchkey git wiring (self-healing a gateway URL whose
reverse-tunneled port changed across restarts) and periodically re-verifies
that the sync repo is still private: the post-commit hook holds its pushes
until visibility is first confirmed private and whenever the repo is confirmed
public. A re-check that fails outright keeps the last confirmed answer and is
retried every tick (see _refresh_visibility).

Workspace data under data/ is NOT synced to GitHub: it is gitignored and
covered by the restic host-backup service instead. Only git commits (the main
repo and worker worktrees) reach the sync repo, via the post-commit hook.

The service only exists when the github-sync skill has enabled sync (it adds
the [program:github-sync] block to system/supervisord.conf).
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from github_sync.config import (
    GithubSyncConfigError,
    load_repo_url,
)
from github_sync.visibility import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNKNOWN,
    check_repo_visibility,
)
from github_sync.wiring import apply_git_wiring

TICK_INTERVAL_SECONDS = 60
# How long a confirmed visibility answer stays fresh before it is re-checked.
# Failed checks are retried every tick instead.
VISIBILITY_CHECK_INTERVAL_SECONDS = 900
LOG_FILE = Path("/tmp/github-sync.log")
# Machine-readable status mirror, read by the post-commit hook (to respect a
# visibility halt) and by the github-sync skill's status report. Lives in /tmp
# deliberately: it is per-boot state, not something to sync. The default path
# is what system/libs/github_sync/git_hooks/post-commit reads.
DEFAULT_STATUS_FILE = Path("/tmp/github-sync-status.json")


class _SyncState:
    """Mutable sync status carried across ticks and mirrored to the status file."""

    def __init__(self) -> None:
        self.repo_url: str | None = None
        self.visibility: str = VISIBILITY_UNKNOWN
        self.visibility_checked_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def is_push_allowed(self) -> bool:
        return self.visibility == VISIBILITY_PRIVATE


def status_file_path() -> Path:
    """The status-mirror path; overridable via env so tests stay isolated."""
    override = os.environ.get("GITHUB_SYNC_STATUS_FILE", "")
    return Path(override) if override else DEFAULT_STATUS_FILE


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_or_none(moment: datetime | None) -> str | None:
    """ISO-8601 with a trailing Z (e.g. 2026-05-06T17:42:13Z), or None."""
    if moment is None:
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _refresh_visibility(state: _SyncState, repo_url: str) -> None:
    """Re-check repo visibility when the last confirmed answer has gone stale.

    A completed check updates the state; a failed check (UNKNOWN result)
    leaves the previous answer in place and is retried next tick. Transitions
    are logged loudly since a repo flipping public is a security condition.
    """
    if (
        state.visibility_checked_at is not None
        and state.visibility != VISIBILITY_UNKNOWN
    ):
        age_seconds = (_now_utc() - state.visibility_checked_at).total_seconds()
        if age_seconds < VISIBILITY_CHECK_INTERVAL_SECONDS:
            return
    visibility = check_repo_visibility(repo_url)
    if visibility == VISIBILITY_UNKNOWN:
        logger.debug("Could not check visibility of {}; will retry", repo_url)
        return
    if visibility != state.visibility:
        if visibility == VISIBILITY_PRIVATE:
            logger.info("Sync repo {} confirmed private; pushes enabled", repo_url)
        else:
            logger.error(
                "Sync repo {} is PUBLIC; halting all sync pushes until it is "
                "made private again",
                repo_url,
            )
    state.visibility = visibility
    state.visibility_checked_at = _now_utc()


def _write_status(state: _SyncState) -> None:
    """Mirror the sync state to the status file for the hook and the skill.

    Written atomically (temp file in the same directory + ``os.replace``) so
    the post-commit hook -- which reads this file concurrently to decide
    whether a visibility halt is in effect -- can never observe a truncated
    file. A truncated read would make jq fail and the hook would default to
    allowing the push, silently defeating the halt.
    """
    payload = {
        "timestamp": _iso_or_none(_now_utc()),
        "repo_url": state.repo_url,
        "visibility": state.visibility,
        "visibility_checked_at": _iso_or_none(state.visibility_checked_at),
        "is_push_allowed": state.is_push_allowed,
        "last_error": state.last_error,
    }
    status_path = status_file_path()
    tmp_path = status_path.with_name(status_path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp_path, status_path)
    except OSError as e:
        logger.warning("Failed to write status file {}: {}", status_path, e)


def _do_tick(state: _SyncState) -> None:
    """Run one watchdog tick: re-apply wiring, verify visibility, mirror status."""
    try:
        repo_url = load_repo_url()
    except GithubSyncConfigError as e:
        logger.error("Invalid sync config: {}", e)
        state.last_error = str(e)
        _write_status(state)
        return
    if repo_url is None:
        logger.warning(
            "github_sync.toml is missing; sync is not configured (run the "
            "github-sync skill), idling"
        )
        _write_status(state)
        return
    if repo_url != state.repo_url:
        if state.repo_url is not None:
            # The confirmed visibility answer belongs to the previously
            # configured repo; a swapped-in repo must earn its own
            # confirmed-private answer before any push.
            logger.info(
                "Sync repo changed from {} to {}; holding pushes until the "
                "new repo is confirmed private",
                state.repo_url,
                repo_url,
            )
            state.visibility = VISIBILITY_UNKNOWN
            state.visibility_checked_at = None
        state.repo_url = repo_url

    # Re-apply the gateway wiring every tick: the gateway URL embeds a
    # reverse-tunneled port that can change across restarts, and the
    # post-commit hook (the only pusher) has no self-heal of its own.
    if not apply_git_wiring():
        state.last_error = "latchkey gateway env incomplete; wiring not applied"
        _write_status(state)
        return
    state.last_error = None

    _refresh_visibility(state, repo_url)
    if state.visibility == VISIBILITY_PUBLIC:
        logger.error(
            "Sync repo {} is PUBLIC; pushes are halted (make it private again "
            "to resume syncing)",
            repo_url,
        )
    elif state.visibility == VISIBILITY_UNKNOWN:
        logger.warning(
            "Sync repo {} visibility not confirmed yet; pushes are held", repo_url
        )
    else:
        pass
    _write_status(state)


def run_forever() -> None:
    """Main loop: keep the wiring fresh and the visibility answer current."""
    # Tee stderr-bound logs into LOG_FILE so operators can `tail` the file
    # across restarts of just this service window. /tmp wipes on container
    # restart, which is the intended scope for the debug log. Set up here
    # rather than at module import so that merely importing this module
    # (e.g. from tests) does not start writing to the log file.
    logger.add(LOG_FILE, level="INFO")

    logger.info("Starting github-sync watchdog (interval={}s)", TICK_INTERVAL_SECONDS)

    state = _SyncState()
    while True:
        _do_tick(state)
        time.sleep(TICK_INTERVAL_SECONDS)
