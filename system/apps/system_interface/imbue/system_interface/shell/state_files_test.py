from pathlib import Path

import pytest

from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic


def test_a_write_lands_whole_and_a_failed_one_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "state" / "projects.json"
    write_json_atomic(target, {"version": 1})
    assert read_json_object(target) == {"version": 1}
    assert sorted(path.name for path in target.parent.iterdir()) == ["projects.json"]

    # A non-empty directory in the file's place makes the rename fail after the temp file was written.
    occupied = tmp_path / "occupied"
    (occupied / "child").mkdir(parents=True)
    with pytest.raises(ShellStateError):
        write_json_atomic(occupied, {"version": 1})
    assert sorted(path.name for path in tmp_path.iterdir()) == ["occupied", "state"]


def test_a_write_under_a_path_that_is_not_a_directory_raises_the_state_error(tmp_path: Path) -> None:
    """The cleanup after a failed write cannot itself fail the write differently: a regular file where
    the directory should be makes the mkdir fail, and the temp file's unlink fails too (not a
    directory), yet the caller still gets the wrapped error and the file is left as it was."""
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("in the way")
    with pytest.raises(ShellStateError, match="cannot write shell state file"):
        write_json_atomic(blocking_file / "projects.json", {"version": 1})
    assert blocking_file.read_text() == "in the way"
