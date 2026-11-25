import errno
import os

import pytest

from files_app.os_refusals import OsRefusal
from files_app.os_refusals import refusal_for_os_error


def _os_error(number: int) -> OSError:
    return OSError(number, os.strerror(number), "/data/notes.txt")


@pytest.mark.parametrize(
    ("number", "status", "kind", "is_routine"),
    [
        (errno.EACCES, 403, "permission_denied", False),
        (errno.EPERM, 403, "permission_denied", False),
        (errno.EROFS, 403, "read_only", False),
        (errno.ENOSPC, 507, "out_of_space", False),
        (errno.EDQUOT, 507, "out_of_space", False),
        (errno.ENOENT, 404, "not_found", False),
        (errno.EEXIST, 409, "already_exists", False),
        (errno.ENOTEMPTY, 409, "already_exists", False),
        (errno.ENAMETOOLONG, 400, "name_too_long", True),
        (errno.ENOTDIR, 409, "not_a_folder", False),
        (errno.EISDIR, 409, "is_a_folder", False),
        (errno.EBUSY, 409, "in_use", True),
        (errno.ETXTBSY, 409, "in_use", True),
        (errno.EFBIG, 413, "too_large", False),
        (errno.ELOOP, 400, "link_loop", False),
    ],
)
def test_an_error_people_can_act_on_is_a_refusal_in_their_words_without_the_path(
    number: int, status: int, kind: str, is_routine: bool
) -> None:
    refusal = refusal_for_os_error(_os_error(number), is_read=False)

    assert isinstance(refusal, OsRefusal)
    # Only a refusal that passes on its own, or that the person puts right by typing, is routine; any other leaves
    # them unable to do what they meant, and the page offers an agent for it.
    assert (refusal.status, refusal.kind, refusal.is_routine) == (status, kind, is_routine)
    assert "notes.txt" not in refusal.sentence


@pytest.mark.parametrize("number", [errno.EIO, errno.EINVAL, None])
def test_an_unexplained_error_is_no_refusal(number: int | None) -> None:
    error = OSError("something odd") if number is None else _os_error(number)

    assert refusal_for_os_error(error, is_read=False) is None
