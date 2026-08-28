import json
from pathlib import Path

import pytest

from imbue.system_interface.member_locations import MemberLocationError
from imbue.system_interface.member_locations import clear_location
from imbue.system_interface.member_locations import read_locations
from imbue.system_interface.member_locations import set_location
from imbue.system_interface.projects import ProjectMemberRefError

_LOCATIONS_FILENAME = "member_locations.json"


def test_an_unbeaconed_workspace_has_no_locations_and_no_file(tmp_path: Path) -> None:
    assert read_locations(tmp_path) == {}
    assert not (tmp_path / _LOCATIONS_FILENAME).exists()


def test_set_location_round_trips(tmp_path: Path) -> None:
    assert set_location(tmp_path, "service:files?instance=files-2", "/notes/2026/") == "/notes/2026/"
    assert read_locations(tmp_path) == {"service:files?instance=files-2": "/notes/2026/"}


def test_locations_survive_a_round_trip_through_the_file(tmp_path: Path) -> None:
    set_location(tmp_path, "service:files?instance=files-1", "/a/")
    set_location(tmp_path, "service:files?instance=files-2", "/b/?q=x")

    assert json.loads((tmp_path / _LOCATIONS_FILENAME).read_text()) == {
        "location_by_ref": {
            "service:files?instance=files-1": "/a/",
            "service:files?instance=files-2": "/b/?q=x",
        }
    }


def test_a_blank_path_clears_the_entry(tmp_path: Path) -> None:
    set_location(tmp_path, "service:files?instance=files-1", "/a/")
    assert set_location(tmp_path, "service:files?instance=files-1", "  ") is None
    assert read_locations(tmp_path) == {}


def test_unrooted_and_protocol_relative_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(MemberLocationError):
        set_location(tmp_path, "service:files?instance=files-1", "notes/")
    with pytest.raises(MemberLocationError):
        set_location(tmp_path, "service:files?instance=files-1", "//evil.example/")
    assert read_locations(tmp_path) == {}


def test_a_path_over_the_cap_is_rejected_rather_than_truncated(tmp_path: Path) -> None:
    with pytest.raises(MemberLocationError):
        set_location(tmp_path, "service:files?instance=files-1", "/" + "a" * 2048)


def test_a_blank_ref_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectMemberRefError):
        set_location(tmp_path, "  ", "/a/")


def test_clear_location_drops_a_destroyed_objects_entry(tmp_path: Path) -> None:
    set_location(tmp_path, "service:files?instance=files-1", "/a/")
    assert clear_location(tmp_path, "service:files?instance=files-1") is True
    assert clear_location(tmp_path, "service:files?instance=files-1") is False
    assert read_locations(tmp_path) == {}


def test_a_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / _LOCATIONS_FILENAME).write_text("{not json")
    assert read_locations(tmp_path) == {}
