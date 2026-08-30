import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from versioning.data_types import RestoreError


@contextmanager
def operation_lock(lock_file: Path) -> Iterator[None]:
    """Serialize write operations (restore, bring-back); a concurrent one fails loudly."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise RestoreError("Another change is already in progress") from e
        yield
