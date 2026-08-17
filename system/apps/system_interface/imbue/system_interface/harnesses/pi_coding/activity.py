"""pi's activity tracker.

pi's transcript carries no turn-boundary markers (like claude, unlike codex), so its
tracker IS claude's -- a real subclass rather than a duplicated copy. Only the process
marker differs: mngr_pi_coding touches ``pi_process_started`` on launch/resume; its
mtime bounds transcript staleness.
"""

from typing import ClassVar

from imbue.system_interface.harnesses.claude.activity import ClaudeActivityTracker


class PiActivityTracker(ClaudeActivityTracker):
    """pi: no turn markers, so activity is claude's lifecycle-plus-tail inference."""

    marker_filename: ClassVar[str] = "pi_process_started"
