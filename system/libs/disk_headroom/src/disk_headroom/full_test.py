import errno
import os

from disk_headroom.full import is_disk_full_error


def _os_error(code: int) -> OSError:
    return OSError(code, os.strerror(code))


def test_a_full_filesystem_and_a_spent_quota_are_both_a_full_disk() -> None:
    assert is_disk_full_error(_os_error(errno.ENOSPC))
    assert is_disk_full_error(_os_error(errno.EDQUOT))


def test_other_write_failures_are_not_a_full_disk() -> None:
    assert not is_disk_full_error(_os_error(errno.EACCES))
    assert not is_disk_full_error(ValueError("bad value"))


class _WrappedError(Exception):
    pass


def test_a_full_disk_wrapped_in_another_error_is_found() -> None:
    try:
        try:
            raise _os_error(errno.ENOSPC)
        except OSError as e:
            raise _WrappedError("cannot write the state file") from e
    except _WrappedError as wrapped:
        assert is_disk_full_error(wrapped)


def test_a_full_disk_hit_while_handling_another_error_is_found() -> None:
    try:
        try:
            raise ValueError("first")
        except ValueError:
            raise _os_error(errno.ENOSPC)
    except OSError as raised:
        outer = RuntimeError("outer")
        outer.__context__ = raised
        assert is_disk_full_error(outer)
