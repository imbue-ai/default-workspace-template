"""A write refused because the disk is full, and the one line the user is told about it."""

import errno
from typing import Final

# ENOSPC is the filesystem out of room; EDQUOT is the cloud machine's disk quota spent first.
DISK_FULL_ERRNOS: Final[frozenset[int]] = frozenset({errno.ENOSPC, errno.EDQUOT})

# HTTP 507 Insufficient Storage: what an app's route answers a request the full disk refused.
DISK_FULL_HTTP_STATUS: Final[int] = 507

DISK_FULL_MESSAGE: Final[str] = (
    "The workspace's disk is full. Delete files you no longer need, then try again."
)


def is_disk_full_error(error: BaseException) -> bool:
    """Whether ``error``, or an error it was raised from or while handling, is a write refused for lack of space."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and current.errno in DISK_FULL_ERRNOS:
            return True
        current = current.__cause__ or current.__context__
    return False
