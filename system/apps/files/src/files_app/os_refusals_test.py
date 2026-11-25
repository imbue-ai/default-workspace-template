import errno
import os
import re
from pathlib import Path
from typing import get_args

import pytest

from files_app.os_refusals import FailureKind
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
        (errno.EEXIST, 409, None, False),
        (errno.ENOTEMPTY, 409, None, False),
        (errno.ENAMETOOLONG, 400, None, True),
        (errno.ENOTDIR, 409, None, False),
        (errno.EISDIR, 409, None, False),
        (errno.EBUSY, 409, None, True),
        (errno.ETXTBSY, 409, None, True),
        (errno.EFBIG, 413, None, False),
        (errno.ELOOP, 400, None, False),
    ],
)
def test_an_error_people_can_act_on_is_a_refusal_in_their_words_without_the_path(
    number: int, status: int, kind: FailureKind | None, is_routine: bool
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


@pytest.mark.frontend
def test_the_frontend_reads_exactly_the_kinds_the_server_sends() -> None:
    """The frontend narrows a refusal's ``kind`` to its own list of the server's kinds, in a language that cannot
    import this one: a kind missing there is told as no kind at all, and one only there is never sent."""
    source = (Path(__file__).parents[2] / "frontend" / "src" / "models" / "failures.ts").read_text()
    declaration = re.search(r"FILES_FAILURE_KINDS = \[(.*?)\] as const", source, re.S)
    assert declaration is not None, "FILES_FAILURE_KINDS is not declared the way this test reads it"

    assert set(re.findall(r'"([a-z_]+)"', declaration.group(1))) == set(get_args(FailureKind))
