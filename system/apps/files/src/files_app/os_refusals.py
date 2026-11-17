"""What the disk's refusals mean to the person using Files: an ``OSError`` whose cause they can act on becomes a
status, a kind the frontend words per action, and a sentence naming only the cause (the frontend's sentence already
names the file). Anything else is a failure nobody explained, which the frontend asks to have reported."""

import errno
from typing import Final
from typing import NamedTuple


class OsRefusal(NamedTuple):
    status: int
    kind: str
    sentence: str


# Refusals to change something the person may not change here; a MOVE answering one says which side refused.
WRITE_REFUSAL_KINDS: Final[frozenset[str]] = frozenset({"permission_denied", "read_only"})

_PERMISSION_DENIED: Final[OsRefusal] = OsRefusal(403, "permission_denied", "You don't have permission to change this.")
# A download's refusal is shown as the server words it, so a refused read must not speak of changing anything.
_PERMISSION_DENIED_TO_READ: Final[OsRefusal] = OsRefusal(
    403, "permission_denied", "You don't have permission to open this."
)
_ALREADY_EXISTS: Final[OsRefusal] = OsRefusal(409, "already_exists", "Something with that name is already there.")
_IN_USE: Final[OsRefusal] = OsRefusal(409, "in_use", "It's in use right now. Try again in a moment.")
_OUT_OF_SPACE: Final[OsRefusal] = OsRefusal(507, "out_of_space", "This workspace is out of disk space.")

_REFUSAL_BY_ERRNO: Final[dict[int, OsRefusal]] = {
    errno.EACCES: _PERMISSION_DENIED,
    errno.EPERM: _PERMISSION_DENIED,
    errno.EROFS: OsRefusal(403, "read_only", "This part of the workspace is read-only."),
    errno.ENOSPC: _OUT_OF_SPACE,
    errno.EDQUOT: _OUT_OF_SPACE,
    errno.ENOENT: OsRefusal(404, "not_found", "It's no longer there."),
    errno.EEXIST: _ALREADY_EXISTS,
    errno.ENOTEMPTY: _ALREADY_EXISTS,
    errno.ENAMETOOLONG: OsRefusal(400, "name_too_long", "That name is too long."),
    errno.ENOTDIR: OsRefusal(409, "not_a_folder", "There's a file where a folder should be."),
    errno.EISDIR: OsRefusal(409, "is_a_folder", "There's a folder where a file should be."),
    errno.EBUSY: _IN_USE,
    errno.ETXTBSY: _IN_USE,
    errno.EFBIG: OsRefusal(413, "too_large", "It's too large."),
    errno.ELOOP: OsRefusal(400, "link_loop", "It links back to itself."),
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
