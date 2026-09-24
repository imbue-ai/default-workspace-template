"""pi's activity tracker.

pi's transcript carries no turn-boundary markers (like claude, unlike codex), so its
tracker IS claude's -- a real subclass rather than a duplicated copy. Only the process
marker differs: mngr_pi_coding touches ``pi_process_started`` on launch/resume; its
mtime bounds transcript staleness. The module also declares pi's startup ready marker,
which is read against that same process marker.
"""

from typing import ClassVar
from typing import Final

from imbue.chat.harnesses.claude.activity import ClaudeActivityTracker
from imbue.chat.harnesses.startup_readiness import StartupReadyMarker

# Written by mngr's pi lifecycle extension on session start and left in place across launches, so
# a stale one is told apart by the process-started marker's mtime. Kept in sync with
# ``_SESSION_STARTED_SENTINEL_NAME`` in mngr_pi_coding's plugin.py.
PI_STARTUP_READY_MARKER: Final[StartupReadyMarker] = StartupReadyMarker(
    filename="pi_session_started", is_deleted_at_launch=False
)


class PiActivityTracker(ClaudeActivityTracker):
    """pi: no turn markers, so activity is claude's lifecycle-plus-tail inference."""

    marker_filename: ClassVar[str] = "pi_process_started"
