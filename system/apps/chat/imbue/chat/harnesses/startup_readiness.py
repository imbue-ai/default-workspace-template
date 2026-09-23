"""Whether a harness's process has launched but does not yet accept input.

A send made in that window waits inside mngr for the harness to come up, which the chat shows
as "Connecting..." rather than as an ordinary send. The harness says when it is up by writing a
marker file in the agent's state dir; this module reads that marker.
"""

from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class StartupReadyMarker(FrozenModel):
    """The file a harness writes in the agent's state dir once a freshly launched process accepts input."""

    filename: str = Field(description="The marker's name within the agent's state dir")
    is_deleted_at_launch: bool = Field(
        description=(
            "Whether the harness's launch deletes the marker before starting the process. When it does not, "
            "a marker older than the launch's process-started marker was left by the previous process."
        )
    )


def _read_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def is_harness_starting_up(
    state_dir: Path, marker: StartupReadyMarker | None, process_started_marker_filename: str
) -> bool:
    """Whether the harness's current process has launched and not yet written its ready marker.

    Always False for a harness that writes no marker: nothing says it is still starting.
    """
    if marker is None:
        return False
    ready_at = _read_mtime(state_dir / marker.filename)
    if ready_at is None:
        return True
    if marker.is_deleted_at_launch:
        return False
    process_started_at = _read_mtime(state_dir / process_started_marker_filename)
    return process_started_at is not None and ready_at < process_started_at
