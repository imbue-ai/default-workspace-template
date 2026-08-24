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

"No longer matches" is judged by *what this process runs*, not by raw HEAD
equality. The workspace repo moves for plenty of reasons that leave this
server perfectly current -- minds commit their ordinary work here constantly,
the apply's own version-history commit lands after the restart, and a
frontend-only apply rebuilds the served bundle without restarting -- so a bare
HEAD comparison would show the banner near-permanently and erode the trust the
real one needs. The moved-tree check therefore diffs the startup HEAD against
the current one and reports staleness only when a changed path is one this
process holds in memory or resolves its environment from (see
:func:`_is_path_relevant_to_this_server`).
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

# Bound on the git reads. rev-parse/diff on a local repo are milliseconds; the
# bound only keeps a wedged git from stalling the app shell.
_GIT_TIMEOUT_SECONDS = 10.0

# What makes THIS running process stale. A deliberate mirror of the update
# apply's restart classification (``plan_apply``/``classify_path`` in
# ``.agents/skills/update-self/scripts/update_self.py`` -- a skill script this
# app cannot import): the backend code held in memory, the manifests its
# environment was resolved from, the vendored mngr it imports in-process and
# shells out to, and the settings file it re-reads with long-lived parsing
# code. Deliberately NOT the frontend (the served bundle is rebuilt on disk
# without a restart), docs, skills, or anything else agents routinely commit.
_APP_BACKEND_PREFIX = "system/apps/system_interface/imbue/"
_VENDORED_MNGR_PREFIX = "system/vendor/mngr/"
_LIVE_SETTINGS_FILE = ".mngr/settings.toml"
_BACKEND_MANIFESTS = frozenset(
    {
        "system/apps/system_interface/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
    }
)


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith("_test.py") or name.startswith("test_")


def _is_path_relevant_to_this_server(path: str) -> bool:
    """Whether a change to ``path`` leaves this running server stale."""
    if path == _LIVE_SETTINGS_FILE or path in _BACKEND_MANIFESTS:
        return True
    if path.startswith(_VENDORED_MNGR_PREFIX):
        return not path.endswith(".md")
    if path.startswith(_APP_BACKEND_PREFIX):
        return path.endswith(".py") and not _is_test_file(path)
    return False


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


def _read_changed_paths(repo_root: Path, since_head: str) -> list[str] | None:
    """The paths whose *content* differs between ``since_head`` and ``HEAD``.

    A tree diff rather than a commit walk, so a change that landed and was then
    reverted (the apply's own rollback commits) correctly reads as unmoved.
    ``None`` when the diff cannot be taken (``since_head`` gone, a wedged git):
    like :func:`_read_head`, everything degrades to "no banner".
    """
    try:
        result = run_local_command_modern_version(
            command=["git", "diff", "--name-only", since_head, "HEAD"],
            cwd=repo_root,
            is_checked=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (ProcessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


class UpdateStalenessTracker(FrozenModel):
    """Remembers the tree HEAD this server started from and compares later.

    Built once per process via :meth:`capture` (a field on
    ``SystemInterfaceState``); ``staleness`` is asked once per rendered app
    shell -- a page load -- which is infrequent enough that a bounded ``git
    rev-parse`` per call costs nothing noticeable. The caller is responsible
    for keeping it to that: the not-built placeholder's ``HEAD`` poll also
    reaches the shell route, ten seconds apart per open tab, and must not ask.
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
        or the same apply is being resumed), not merely "the tree moved". The
        moved-tree comparison itself is scoped to paths that leave THIS
        process stale (see the module docstring): the workspace repo moves
        constantly for reasons -- ordinary agent commits, the apply's own
        ledger commit, a frontend-only apply -- that this server is fully
        current for.
        """
        if (self.repo_root / UPDATE_APPLY_MARKER_REL).exists():
            return STALENESS_UPDATE_INTERRUPTED
        if self.startup_head is None:
            return None
        current = _read_head(self.repo_root)
        if current is None or current == self.startup_head:
            return None
        changed = _read_changed_paths(self.repo_root, self.startup_head)
        if changed is None:
            return None
        if any(_is_path_relevant_to_this_server(path) for path in changed):
            return STALENESS_TREE_MOVED
        return None
