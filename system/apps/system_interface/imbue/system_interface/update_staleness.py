"""Detect when the served tree has moved under this running server.

An update lands by advancing the working tree; the running server only becomes
consistent with it once it restarts into the merged code. The atomic update
apply does both in one motion, but two states can still leave a live process
serving old code over new on-disk state: an apply interrupted mid-motion (its
marker is still present), and a tree that advanced without an activation (an
emergency-path apply, or a hand merge outside the flow). Both are exactly the
skew the geebspace incident grew from, so the server says so instead of
serving silently: it records the tree HEAD it started from, and the app shell
gets a meta tag whenever the live tree no longer
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
from pydantic import PrivateAttr

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

# The workspace root this server serves from, walked back out of
# ``system/apps/system_interface/imbue/system_interface``. Declared here rather
# than in ``server.py`` (which imports it back) so the walk exists once: two
# copies of a ``parents[5]`` count break silently when a directory level moves.
WORKSPACE_ROOT_DIRECTORY = Path(__file__).resolve().parents[5]

# The update apply's in-flight marker (see
# ``.agents/skills/update-self/scripts/update_self.py``): present exactly while
# an apply is mid-motion or was interrupted before recovery.
UPDATE_APPLY_MARKER_REL = "data/.state/update-apply/marker.json"
# The apply's emergency record: written when a rollback could not put a healthy
# workspace back, and cleared only when a later apply or recovery confirms one.
# Unlike the marker it is not transient -- it names a workspace that needs a
# person -- so it outranks both other variants.
UPDATE_APPLY_EMERGENCY_REL = "data/.state/update-apply/emergency.json"

# The three staleness variants, also the values of the meta tag the frontend
# renders its banner from.
STALENESS_UPDATE_EMERGENCY = "update-emergency"
STALENESS_UPDATE_INTERRUPTED = "update-interrupted"
STALENESS_TREE_MOVED = "updated-not-activated"

# The meta tag the built app shell carries, and the frontend renders its
# banner from, whenever the live tree no longer matches the code this process
# is running; absent when consistent.
UPDATE_STALENESS_META_TAG = "system-interface-update-staleness"

# Bound on the git reads. rev-parse/diff on a local repo are milliseconds; the
# bound only keeps a wedged git from stalling the app shell.
_GIT_TIMEOUT_SECONDS = 10.0
# How long a timed-out git gets between SIGTERM and SIGKILL. A read-only git
# holds nothing worth a graceful exit, and this runs on a request thread.
_GIT_SHUTDOWN_TIMEOUT_SECONDS = 1.0

# What makes THIS running process stale: the code it holds in memory, the
# manifests its environment was resolved from, and the settings file it
# re-reads with long-lived parsing code. Deliberately NOT the frontend (the
# served bundle is rebuilt on disk without a restart), docs, skills, tests, or
# anything else agents routinely commit. (The update apply itself restarts the
# services agent on every apply, so it keeps no such rule; this one exists for
# a tree moved by anything else.) The vendored mngr is read at runtime through
# more than its ``.py`` files (this process both imports it and shells out to
# it), so everything there but docs and tests counts -- a missed skew is the
# failure this whole detector exists to prevent.
#
# The imported-source prefixes are every workspace tree this process runs code
# from: its own backend, the vendored mngr (imported in-process and shelled
# out to), the OOM banding library (``agent_manager``, ``oom_prioritizer``) and
# the tk command parser (the claude/codex/pi-coding tool labels). All are
# editable installs resolving straight into these trees, so the moment one
# advances this process is running old code. ``test_every_imported_workspace_
# package_is_covered`` holds this list to the app's actual dependencies.
_APP_BACKEND_PREFIX = "system/apps/system_interface/imbue/"
_VENDORED_MNGR_PREFIX = "system/vendor/mngr/"
_IMPORTED_SOURCE_PREFIXES = (
    _APP_BACKEND_PREFIX,
    "system/services/oom_priority/",
    "system/libs/tk_command_parsing/",
)
_LIVE_SETTINGS_FILE = ".mngr/settings.toml"
_BACKEND_MANIFESTS = frozenset(
    {
        "system/apps/system_interface/pyproject.toml",
        "system/services/oom_priority/pyproject.toml",
        "system/libs/tk_command_parsing/pyproject.toml",
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
        return not path.endswith(".md") and not _is_test_file(path)
    if path.startswith(_IMPORTED_SOURCE_PREFIXES):
        return path.endswith(".py") and not _is_test_file(path)
    return False


def _read_git(command: list[str], repo_root: Path) -> str | None:
    """Run a read-only git command, or ``None`` when it could not be run.

    Everything degrades to ``None``: a workspace where git cannot be read (no
    git, a corrupt repo, a wedged index) has bigger problems than a missing
    banner, and a false banner would erode the trust the real one needs. But
    the degrading is logged, because this feature exists to make a silent skew
    visible and a permanently silent detector is the worst way for it to fail.
    """
    try:
        result = run_local_command_modern_version(
            command=command,
            cwd=repo_root,
            is_checked=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            shutdown_timeout_sec=_GIT_SHUTDOWN_TIMEOUT_SECONDS,
        )
    except (ProcessError, OSError) as e:
        logger.warning(
            "update-staleness: `{}` could not run in {} ({}); no banner this request.",
            " ".join(command),
            repo_root,
            e,
        )
        return None
    if result.returncode != 0:
        logger.warning(
            "update-staleness: `{}` exited {} in {} ({}); no banner this request.",
            " ".join(command),
            result.returncode,
            repo_root,
            result.stderr.strip()[-300:],
        )
        return None
    return result.stdout


def _read_head(repo_root: Path) -> str | None:
    """The tree HEAD at ``repo_root``, or ``None`` when it cannot be read."""
    output = _read_git(["git", "rev-parse", "HEAD"], repo_root)
    return output.strip() or None if output is not None else None


def _read_changed_paths(repo_root: Path, since_head: str) -> list[str] | None:
    """The paths whose *content* differs between ``since_head`` and ``HEAD``.

    A tree diff rather than a commit walk, so a change that landed and was then
    reverted (the apply's own rollback commits) correctly reads as unmoved.
    ``None`` when the diff cannot be taken (``since_head`` gone, a wedged git):
    like :func:`_read_head`, everything degrades to "no banner".
    """
    # ``-z``: without it git C-quotes any path with a non-ASCII byte
    # (``"system/vendor/mngr/.../l\303\257st.py"``), which then starts with a
    # quote and matches no prefix rule.
    output = _read_git(["git", "diff", "--name-only", "-z", since_head, "HEAD"], repo_root)
    if output is None:
        return None
    return [path for path in output.split("\0") if path]


class UpdateStalenessTracker(FrozenModel):
    """Remembers the tree HEAD this server started from and compares later.

    Built once per process via :meth:`capture` (a field on
    ``SystemInterfaceState``); ``staleness`` is asked on every app-shell
    ``GET`` -- the root route and its catch-all for client-side routes, so a
    page load rather than the app's API traffic. Each ask costs one bounded
    ``git rev-parse``; the tree diff behind the moved-tree verdict runs only
    when ``HEAD`` differs from the last ask, since the verdict for a given
    ``HEAD`` cannot change (it is a content comparison against a fixed
    baseline). The caller keeps the not-built placeholder's ``HEAD`` poll --
    ten seconds apart per open tab -- from asking at all.
    """

    repo_root: Path = Field(description="The workspace root this server serves from")
    startup_head: str | None = Field(
        description="The tree HEAD when this process started; None disables the "
        "moved-tree comparison (the marker check still applies)"
    )
    # The last (current HEAD, moved-tree verdict) pair. Written as one tuple,
    # so a race between request threads only repeats the diff.
    _moved_tree_verdict: tuple[str, bool] | None = PrivateAttr(default=None)

    @classmethod
    def capture(cls, repo_root: Path = WORKSPACE_ROOT_DIRECTORY) -> "UpdateStalenessTracker":
        """Snapshot the current tree HEAD as this process's startup baseline."""
        startup_head = _read_head(repo_root)
        if startup_head is None:
            logger.warning(
                "Could not read the tree HEAD at startup; update-staleness detection is disabled for this process."
            )
        return cls(repo_root=repo_root, startup_head=startup_head)

    def staleness(self) -> str | None:
        """The staleness variant to surface, or ``None`` when consistent.

        The emergency record outranks everything: a rollback that could not
        restore health is the one state here that will not resolve itself, and
        it is invisible to the other two checks -- it comes with no marker (the
        apply clears it on the way out) and a tree the rollback has already put
        back, so both would read as consistent.

        The marker outranks the moved-tree comparison: while it exists the
        honest description is "an update was interrupted" (recovery is coming,
        or the same apply is being resumed), not merely "the tree moved". The
        moved-tree comparison itself is scoped to paths that leave THIS
        process stale (see the module docstring): the workspace repo moves
        constantly for reasons -- ordinary agent commits, the apply's own
        ledger commit, a frontend-only apply -- that this server is fully
        current for.
        """
        if (self.repo_root / UPDATE_APPLY_EMERGENCY_REL).exists():
            return STALENESS_UPDATE_EMERGENCY
        if (self.repo_root / UPDATE_APPLY_MARKER_REL).exists():
            return STALENESS_UPDATE_INTERRUPTED
        if self.startup_head is None:
            return None
        current = _read_head(self.repo_root)
        if current is None or current == self.startup_head:
            return None
        return STALENESS_TREE_MOVED if self._has_tree_moved_for_this_server(current) else None

    def _has_tree_moved_for_this_server(self, current_head: str) -> bool:
        cached = self._moved_tree_verdict
        if cached is not None and cached[0] == current_head:
            return cached[1]
        assert self.startup_head is not None
        changed = _read_changed_paths(self.repo_root, self.startup_head)
        if changed is None:
            # Not cached: an unreadable diff is a transient to retry, not a
            # verdict.
            return False
        is_moved = any(_is_path_relevant_to_this_server(path) for path in changed)
        self._moved_tree_verdict = (current_head, is_moved)
        return is_moved
