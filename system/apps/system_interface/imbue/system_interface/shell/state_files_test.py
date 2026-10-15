import errno
from pathlib import Path

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.errors import NOT_SAVED_SENTENCE
from imbue.system_interface.shell.errors import OUT_OF_SPACE_SENTENCE
from imbue.system_interface.shell.errors import RefusalKind
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import state_write_error
from imbue.system_interface.shell.state_files import write_json_atomic


def test_a_write_lands_whole_and_a_failed_one_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "state" / "desktops.json"
    write_json_atomic(target, {"version": 1})
    assert read_json_object(target) == {"version": 1}
    assert sorted(path.name for path in target.parent.iterdir()) == ["desktops.json"]

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
    with pytest.raises(ShellStateError, match="cannot write shell state file") as raised:
        write_json_atomic(blocking_file / "desktops.json", {"version": 1})
    assert blocking_file.read_text() == "in the way"
    assert raised.value.user_facing_message == NOT_SAVED_SENTENCE and raised.value.kind is RefusalKind.NOT_SAVED


@pytest.mark.parametrize("full", [errno.ENOSPC, errno.EDQUOT])
def test_a_write_that_finds_the_disk_full_says_the_workspace_is_out_of_space(tmp_path: Path, full: int) -> None:
    path = tmp_path / "desktops.json"
    error = state_write_error(path, OSError(full, "No space left on device"))
    assert error.user_facing_message == OUT_OF_SPACE_SENTENCE
    assert error.kind is RefusalKind.OUT_OF_SPACE
    # The path and the errno stay in the developer's reason, for the log and "See details".
    assert str(error) == f"cannot write shell state file {path}: [Errno {full}] No space left on device"


class _VersionedDocument(FrozenModel):
    """A stand-in for the stores' documents: a version and one payload field."""

    version: int = Field(description="The file format version")
    entries: dict[str, int] = Field(description="The payload")


def test_a_versioned_document_parses_only_at_its_version_and_shape(tmp_path: Path) -> None:
    path = tmp_path / "entries.json"
    parsed = parse_versioned_document({"version": 3, "entries": {"a": 1}}, _VersionedDocument, 3, path)
    assert parsed == _VersionedDocument(version=3, entries={"a": 1})
    assert parse_versioned_document(None, _VersionedDocument, 3, path) is None
    assert parse_versioned_document({"version": 2, "entries": {}}, _VersionedDocument, 3, path) is None
    assert parse_versioned_document({"entries": {}}, _VersionedDocument, 3, path) is None
    assert parse_versioned_document({"version": 3, "entries": "nope"}, _VersionedDocument, 3, path) is None
