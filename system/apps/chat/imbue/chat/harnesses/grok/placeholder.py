"""Grok Build's placeholder activity tracker. Delete with the rest of the placeholder
wiring when this harness lands its real tracker (see ``harnesses/placeholder.py``)."""

from typing import ClassVar

from imbue.chat.harnesses.placeholder import PlaceholderActivityTracker
from imbue.mngr_grok.api import PROCESS_STARTED_MARKER_FILENAME


class GrokPlaceholderActivityTracker(PlaceholderActivityTracker):
    # mngr_grok's launch script touches this on every launch, so its mtime marks the current grok process.
    marker_filename: ClassVar[str] = PROCESS_STARTED_MARKER_FILENAME
