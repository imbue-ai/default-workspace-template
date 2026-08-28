"""OpenCode's placeholder activity tracker. Delete with the rest of the placeholder
wiring when this harness lands its real tracker (see ``harnesses/placeholder.py``)."""

from typing import ClassVar

from imbue.system_interface.harnesses.placeholder import PlaceholderActivityTracker


class OpenCodePlaceholderActivityTracker(PlaceholderActivityTracker):
    # mngr_opencode writes this readiness sentinel at launch (its
    # ``opencode_config.READY_SENTINEL_FILENAME``), and the launch script clears any stale
    # one before (re)starting -- so its mtime bounds the current process the same way the
    # other harnesses' ``*_process_started`` markers do.
    marker_filename: ClassVar[str] = "opencode_ready"
