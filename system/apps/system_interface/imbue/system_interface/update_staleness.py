"""Detect when the served tree has moved under this running server.

An update lands by advancing the working tree; the running server only becomes
consistent with it once it restarts into the merged code. The atomic update
apply does both in one motion, but two states can still leave a live process
serving old code over new on-disk state: an apply interrupted mid-motion (its
marker is still present), and a tree that advanced without an activation (an
emergency-path apply, or a hand merge outside the flow). Both are exactly the
skew the geebspace incident grew from, so the server says so instead of
serving silently: it records the tree HEAD it started from, and the app shell
gets a response header plus a meta tag whenever the live tree no longer
matches -- an informational banner for the user; acting on it stays with the
agent.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

# The workspace root this server serves from, walked back out of
# ``system/apps/system_interface/imbue/system_interface``.
_WORKSPACE_ROOT_DIRECTORY = Path(__file__).resolve().parents[5]

# The update apply's in-flight marker (see
# ``.agents/skills/update-self/scripts/update_self.py``): present exactly while
# an apply is mid-motion or was interrupted before recovery.
UPDATE_APPLY_MARKER_REL = "data/.state/update-apply/marker.json"

# The two staleness variants, also the values of the response header and the
# meta tag the frontend renders its banner from.
STALENESS_UPDATE_INTERRUPTED = "update-interrupted"
STALENESS_TREE_MOVED = "updated-not-activated"

# Stamped on app-shell responses whenever the live tree no longer matches the
# code this process is running; absent when consistent.
UPDATE_STALENESS_HEADER = "X-Workspace-Update-Staleness"
# The meta tag the frontend reads (mirrors the header value at shell render).
UPDATE_STALENESS_META_TAG = "system-interface-update-staleness"

# Bound on the HEAD reads. rev-parse on a local repo is milliseconds; the
# bound only keeps a wedged git from stalling the app shell.
_GIT_TIMEOUT_SECONDS = 10.0


def _read_head(repo_root: Path) -> str | None:
    """The tree HEAD at ``repo_root``, or ``None`` when it cannot be read.

    Everything degrades to ``None``: a workspace where HEAD cannot be read (no
    git, a corrupt repo) has bigger problems than a missing banner, and a
    false banner would erode the trust the real one needs.
    """
    try:
        result = run_local_command_modern_version(
            command=["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            is_checked=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (ProcessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


class UpdateStalenessTracker(FrozenModel):
    """Remembers the tree HEAD this server started from and compares later.

    Built once per process via :meth:`capture` (a field on
    ``SystemInterfaceState``); ``staleness`` is asked per app-shell request,
    which is a page load -- infrequent enough that a bounded ``git rev-parse``
    per call costs nothing noticeable.
    """

    repo_root: Path = Field(description="The workspace root this server serves from")
    startup_head: str | None = Field(
        description="The tree HEAD when this process started; None disables the "
        "moved-tree comparison (the marker check still applies)"
    )

    @classmethod
    def capture(cls, repo_root: Path = _WORKSPACE_ROOT_DIRECTORY) -> "UpdateStalenessTracker":
        """Snapshot the current tree HEAD as this process's startup baseline."""
        startup_head = _read_head(repo_root)
        if startup_head is None:
            logger.warning(
                "Could not read the tree HEAD at startup; update-staleness "
                "detection is disabled for this process."
            )
        return cls(repo_root=repo_root, startup_head=startup_head)

    def staleness(self) -> str | None:
        """The staleness variant to surface, or ``None`` when consistent.

        The marker outranks the moved-tree comparison: while it exists the
        honest description is "an update was interrupted" (recovery is coming,
        or the same apply is being resumed), not merely "the tree moved".
        """
        if (self.repo_root / UPDATE_APPLY_MARKER_REL).exists():
            return STALENESS_UPDATE_INTERRUPTED
        if self.startup_head is None:
            return None
        current = _read_head(self.repo_root)
        if current is not None and current != self.startup_head:
            return STALENESS_TREE_MOVED
        return None
