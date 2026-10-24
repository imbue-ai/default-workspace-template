"""Whether the collector has read every event file to its end, which ``GET /health`` reports.

Until the first pass that reads every file to its end, the store is still being
built from the files (a first start, or a rebuild after its database was
deleted), so totals read from it are incomplete.
"""

import threading


class IngestStatus:
    def __init__(self) -> None:
        self._caught_up = threading.Event()

    @property
    def is_catching_up(self) -> bool:
        return not self._caught_up.is_set()

    def record_pass(self, is_complete: bool) -> None:
        """Note one pass; a single incomplete pass after catching up means the files outgrew a pass again."""
        if is_complete:
            self._caught_up.set()
        else:
            self._caught_up.clear()
