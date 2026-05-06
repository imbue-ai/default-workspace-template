"""Runtime backup service.

Polls runtime/ every TICK_INTERVAL_SECONDS; if there are uncommitted changes,
makes a backup commit on the orphan branch checked out at runtime/ and (when
GH_TOKEN is set) pushes it to origin.

The orphan branch (mindsbackup/$MNGR_AGENT_ID) and the worktree at runtime/
are created by libs/bootstrap during its pre-services init step, so this
service can assume runtime/ is already a git worktree on that branch.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RUNTIME_DIR = Path("runtime")
TICK_INTERVAL_SECONDS = 60
LOG_FILE = Path("/tmp/runtime-backup.log")


def _log(message: str) -> None:
    """Write a line to stderr and append it to the debug log file."""
    line = f"[runtime-backup] {message}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    try:
        with LOG_FILE.open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        # Log file is best-effort; never let logging fail the loop.
        pass


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the runtime worktree, never raising."""
    return subprocess.run(
        ["git", "-C", str(RUNTIME_DIR), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _has_uncommitted_changes() -> bool:
    """Return True if runtime/ has anything to commit."""
    result = _git("status", "--porcelain")
    if result.returncode != 0:
        _log(f"git status failed (rc={result.returncode}): {result.stderr.strip()}")
        return False
    return bool(result.stdout.strip())


def _now_iso_utc() -> str:
    """Current UTC time as ISO-8601 with a trailing Z (e.g. 2026-05-06T17:42:13Z)."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _do_tick(should_push: bool) -> None:
    """Run one backup tick: add, commit-if-dirty, push-if-token."""
    add_result = _git("add", "-A")
    if add_result.returncode != 0:
        _log(
            f"git add failed (rc={add_result.returncode}): {add_result.stderr.strip()}"
        )
        return

    if _has_uncommitted_changes():
        commit_result = _git("commit", "-m", f"runtime backup: {_now_iso_utc()}")
        if commit_result.returncode != 0:
            _log(
                f"git commit failed (rc={commit_result.returncode}): "
                f"{commit_result.stderr.strip()}"
            )
            return

    if should_push:
        # Always attempt push: covers the case where a prior tick committed but
        # failed to push, so the next tick still ships the unpushed commit.
        push_result = _git("push")
        if push_result.returncode != 0:
            _log(
                f"git push failed (rc={push_result.returncode}): "
                f"{push_result.stderr.strip()}"
            )


def main() -> None:
    """Main loop: poll runtime/ on a fixed interval and back up changes."""
    _log(f"starting (interval={TICK_INTERVAL_SECONDS}s)")

    if not (RUNTIME_DIR / ".git").exists():
        _log("runtime/ is not a git worktree; bootstrap should have created it")

    has_token = bool(os.environ.get("GH_TOKEN"))
    if not has_token:
        _log("no GH_TOKEN; will commit locally but skip push")

    while True:
        _do_tick(should_push=has_token)
        time.sleep(TICK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
