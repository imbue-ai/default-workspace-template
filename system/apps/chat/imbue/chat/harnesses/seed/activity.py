from typing import ClassVar

from imbue.chat.harnesses.placeholder import PlaceholderActivityTracker


class SeedActivityTracker(PlaceholderActivityTracker):
    """The dot of a seed segment's pseudo-agent: no process ever runs it, so no marker is ever touched and it reads idle."""

    marker_filename: ClassVar[str] = "seed_process_started"
