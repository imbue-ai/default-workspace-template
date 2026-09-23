from pathlib import Path

import pytest

from getting_started.errors import StateFileError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic


def test_a_document_round_trips_and_a_missing_or_malformed_file_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    assert read_json_object(path) is None
    write_json_atomic(path, {"is_delivered": True})
    assert read_json_object(path) == {"is_delivered": True}
    path.write_text("[1, 2]")
    assert read_json_object(path) is None
    path.write_text("{not json")
    assert read_json_object(path) is None


def test_a_write_into_a_file_where_a_directory_is_needed_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    with pytest.raises(StateFileError):
        write_json_atomic(blocker / "state.json", {})
