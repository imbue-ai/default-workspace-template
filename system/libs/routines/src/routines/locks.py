import fcntl
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path

from routines.errors import RunInFlightError


@contextmanager
def _flocked(path: Path, is_waited: bool) -> Generator[bool, None, None]:
    """Open the lock file and take its flock for the block, waiting for the holder or not; yields whether it is held.

    The handle is closed on the way out, which releases the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if is_waited else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        handle.close()


@contextmanager
def held_lock(
    path: Path,
    what_is_held: str,
    retry_pauses: Sequence[float] = (),
    pause: Callable[[float], None] | None = None,
) -> Generator[None, None, None]:
    """Hold the file lock for the block; raises RunInFlightError when another process holds it.

    With ``retry_pauses``, a held lock is tried again after each pause (through ``pause``) before it counts as
    held: a holder that has it for an instant, such as a check that takes it to answer whether it is free, does
    not refuse a run.
    """
    for seconds in retry_pauses:
        with _flocked(path, is_waited=False) as is_held:
            if is_held:
                yield
                return
        assert pause is not None, "retry pauses need a way to pause"
        pause(seconds)
    with _flocked(path, is_waited=False) as is_held:
        if not is_held:
            raise RunInFlightError(f"{what_is_held} is already going.")
        yield


@contextmanager
def tried_lock(path: Path) -> Generator[bool, None, None]:
    """Hold the file lock for the block when it is free; yields whether it was taken, never waiting."""
    with _flocked(path, is_waited=False) as is_held:
        yield is_held


@contextmanager
def waited_lock(path: Path) -> Generator[None, None, None]:
    """Hold the file lock for the block, waiting for whoever holds it; for the short state writes."""
    with _flocked(path, is_waited=True):
        yield
