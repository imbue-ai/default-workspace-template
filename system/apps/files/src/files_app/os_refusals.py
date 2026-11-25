"""What the disk's refusals mean to the person using Files: an ``OSError`` whose cause they can act on becomes a
status and a sentence naming only the cause (the frontend's sentence already names the file), and, where the frontend
reads it, a kind. Anything else is a failure nobody explained, which the frontend can only say to try again.

A sentence the server says in more than one place is defined here once and raised from here."""

import errno
from typing import Final
from typing import Literal
from typing import NamedTuple

# The kinds of refusal the frontend reads (its ``FILES_FAILURE_KINDS`` in ``models/failures.ts``): one it words per
# operation, one it tells as an item gone, one it offers a different remedy for. A refusal of any other cause carries
# its sentence alone.
FailureKind = Literal[
    "not_found",
    "permission_denied",
    "read_only",
    "out_of_space",
    "destination_not_writable",
    "source_not_writable",
    "shared_read_only",
]

# Which end of a move: the folder the item is in, or the folder it goes into.
MoveSide = Literal["source", "destination"]


class OsRefusal(NamedTuple):
    status: int
    sentence: str
    kind: FailureKind | None = None
    # Ordinary operation the sentence fully explains, so the person is owed no more help than it gives.
    is_routine: bool = False


GONE: Final[OsRefusal] = OsRefusal(404, "It's no longer there.", "not_found")
ALREADY_EXISTS: Final[OsRefusal] = OsRefusal(409, "Something with that name is already there.")
# A MOVE onto something is refused as WebDAV refuses one sent with ``Overwrite: F``.
NOT_REPLACED: Final[OsRefusal] = ALREADY_EXISTS._replace(status=412)
FOLDER_IN_THE_WAY: Final[OsRefusal] = OsRefusal(409, "There's already a folder with that name.")

# Refusals to change something the person may not change here; a MOVE answering one says which side refused.
WRITE_REFUSAL_KINDS: Final[frozenset[FailureKind]] = frozenset({"permission_denied", "read_only"})
# Which end of a MOVE refused for want of write access, so the page can say which folder that was. Either may be a
# folder without write permission or on a read-only file system, so neither says which.
MOVE_REFUSAL_BY_SIDE: Final[dict[MoveSide, OsRefusal]] = {
    "destination": OsRefusal(
        403, "You can't add things to the folder it would move into.", "destination_not_writable"
    ),
    "source": OsRefusal(403, "You can't take things out of the folder it's in.", "source_not_writable"),
}

_PERMISSION_DENIED: Final[OsRefusal] = OsRefusal(403, "You don't have permission to change this.", "permission_denied")
# A download's refusal is shown as the server words it, so a refused read must not speak of changing anything.
_PERMISSION_DENIED_TO_READ: Final[OsRefusal] = _PERMISSION_DENIED._replace(
    sentence="You don't have permission to open this."
)
_IN_USE: Final[OsRefusal] = OsRefusal(409, "It's in use right now. Try again in a moment.", is_routine=True)
_OUT_OF_SPACE: Final[OsRefusal] = OsRefusal(507, "This workspace is out of disk space.", "out_of_space")

_REFUSAL_BY_ERRNO: Final[dict[int, OsRefusal]] = {
    errno.EACCES: _PERMISSION_DENIED,
    errno.EPERM: _PERMISSION_DENIED,
    errno.EROFS: OsRefusal(403, "This part of the workspace is read-only.", "read_only"),
    errno.ENOSPC: _OUT_OF_SPACE,
    errno.EDQUOT: _OUT_OF_SPACE,
    errno.ENOENT: GONE,
    errno.EEXIST: ALREADY_EXISTS,
    errno.ENOTEMPTY: ALREADY_EXISTS,
    errno.ENAMETOOLONG: OsRefusal(400, "That name is too long.", is_routine=True),
    errno.ENOTDIR: OsRefusal(409, "There's a file where a folder should be."),
    errno.EISDIR: FOLDER_IN_THE_WAY,
    errno.EBUSY: _IN_USE,
    errno.ETXTBSY: _IN_USE,
    errno.EFBIG: OsRefusal(413, "It's too large."),
    errno.ELOOP: OsRefusal(400, "It links back to itself."),
}


def refusal_for_os_error(error: OSError, is_read: bool) -> OsRefusal | None:
    """The refusal ``error`` is to a person, or None for an error with no cause they can act on (EIO and the like);
    ``is_read`` says the request only read, which a permission refusal then says."""
    if error.errno is None:
        return None
    refusal = _REFUSAL_BY_ERRNO.get(error.errno)
    if is_read and refusal == _PERMISSION_DENIED:
        return _PERMISSION_DENIED_TO_READ
    return refusal
