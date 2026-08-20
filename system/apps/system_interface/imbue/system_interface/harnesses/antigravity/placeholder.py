"""Antigravity's placeholder activity tracker. Delete with the rest of the placeholder
wiring when this harness lands its real tracker (see ``harnesses/placeholder.py``)."""

from typing import ClassVar

from imbue.system_interface.harnesses.placeholder import PlaceholderActivityTracker


class AntigravityPlaceholderActivityTracker(PlaceholderActivityTracker):
    # NOTE: mngr_antigravity does not write this marker yet -- unlike mngr_claude
    # (``claude_process_started``), mngr_codex and mngr_pi_coding, it writes no
    # startup/resume marker at all. Naming the file it SHOULD write keeps this honest:
    # while it is absent ``_read_process_started_at`` returns None and the shared
    # transcript-staleness gate is simply inert, which costs nothing here because there is
    # no transcript to call stale. Adding the marker on the mngr side is part of landing
    # antigravity's real tracker.
    marker_filename: ClassVar[str] = "antigravity_process_started"
