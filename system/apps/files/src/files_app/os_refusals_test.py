import errno
import os

import pytest

from files_app.os_refusals import OsRefusal
from files_app.os_refusals import refusal_for_os_error


def _os_error(number: int) -> OSError:
    return OSError(number, os.strerror(number), "/data/notes.txt")


@pytest.mark.parametrize(
    ("number", "status", "kind"),
    [
        (errno.EACCES, 403, "permission_denied"),
        (errno.EPERM, 403, "permission_denied"),
        (errno.EROFS, 403, "read_only"),
        (errno.ENOSPC, 507, "out_of_space"),
        (errno.EDQUOT, 507, "out_of_space"),
        (errno.ENOENT, 404, "not_found"),
        (errno.EEXIST, 409, "already_exists"),
        (errno.ENOTEMPTY, 409, "already_exists"),
        (errno.ENAMETOOLONG, 400, "name_too_long"),
        (errno.ENOTDIR, 409, "not_a_folder"),
        (errno.EISDIR, 409, "is_a_folder"),
        (errno.EBUSY, 409, "in_use"),
        (errno.ETXTBSY, 409, "in_use"),
        (errno.EFBIG, 413, "too_large"),
        (errno.ELOOP, 400, "link_loop"),
    ],
)
def test_an_error_people_can_act_on_is_a_refusal_in_their_words_without_the_path(
    number: int, status: int, kind: str
) -> None:
    refusal = refusal_for_os_error(_os_error(number))

    assert isinstance(refusal, OsRefusal)
    assert (refusal.status, refusal.kind) == (status, kind)
    assert "notes.txt" not in refusal.sentence


@pytest.mark.parametrize("number", [errno.EIO, errno.EINVAL, None])
def test_an_unexplained_error_is_left_to_be_reported(number: int | None) -> None:
    error = OSError("something odd") if number is None else _os_error(number)

    assert refusal_for_os_error(error) is None
