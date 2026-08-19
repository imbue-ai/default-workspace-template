import json
from pathlib import Path

import pytest

from imbue.system_interface.member_titles import MAX_MEMBER_TITLE_LENGTH
from imbue.system_interface.member_titles import MemberTitleLengthError
from imbue.system_interface.member_titles import clear_title
from imbue.system_interface.member_titles import read_titles
from imbue.system_interface.member_titles import set_title
from imbue.system_interface.projects import ProjectMemberRefError

_TITLES_FILENAME = "member_titles.json"


def test_an_unrenamed_workspace_has_no_titles_and_no_file(tmp_path: Path) -> None:
    assert read_titles(tmp_path) == {}
    assert not (tmp_path / _TITLES_FILENAME).exists()


def test_set_title_round_trips(tmp_path: Path) -> None:
    assert set_title(tmp_path, "service:docs-viewer", "Docs") == "Docs"
    assert read_titles(tmp_path) == {"service:docs-viewer": "Docs"}
    assert (tmp_path / _TITLES_FILENAME).exists()


def test_titles_survive_a_round_trip_through_the_file(tmp_path: Path) -> None:
    # The map is the machine's, so it has to outlive the process that wrote it:
    # every read goes back to the file rather than to anything held in memory.
    set_title(tmp_path, "chat:agent-7", "Planning")
    set_title(tmp_path, "terminal:build", "Build logs")
    set_title(tmp_path, "service:browser?session=2", "Docs browser")

    assert read_titles(tmp_path) == {
        "chat:agent-7": "Planning",
        "terminal:build": "Build logs",
        "service:browser?session=2": "Docs browser",
    }


def test_a_title_is_stored_by_ref_and_nowhere_near_a_project(tmp_path: Path) -> None:
    # A title is keyed by the object and by nothing else, which is the whole
    # point: the object renamed in one view reads the same in every other one,
    # and the projects registry is not even created by naming something.
    set_title(tmp_path, "service:docs-viewer", "Docs")

    assert json.loads((tmp_path / _TITLES_FILENAME).read_text()) == {"title_by_ref": {"service:docs-viewer": "Docs"}}
    assert not (tmp_path / "projects_meta.json").exists()


def test_setting_a_title_again_overwrites_it(tmp_path: Path) -> None:
    set_title(tmp_path, "terminal:build", "Build logs")
    assert set_title(tmp_path, "terminal:build", "Deploy logs") == "Deploy logs"
    assert read_titles(tmp_path) == {"terminal:build": "Deploy logs"}


def test_setting_the_same_title_twice_is_idempotent(tmp_path: Path) -> None:
    assert set_title(tmp_path, "terminal:build", "Build logs") == "Build logs"
    assert set_title(tmp_path, "terminal:build", "Build logs") == "Build logs"
    assert read_titles(tmp_path) == {"terminal:build": "Build logs"}


def test_a_title_is_trimmed(tmp_path: Path) -> None:
    assert set_title(tmp_path, "  terminal:build  ", "  Build logs  ") == "Build logs"
    assert read_titles(tmp_path) == {"terminal:build": "Build logs"}


def test_an_empty_title_clears_the_entry_rather_than_storing_a_blank(tmp_path: Path) -> None:
    # There is no such thing as an object named "": an editor emptied and
    # committed puts the object back to whatever it calls itself.
    set_title(tmp_path, "terminal:build", "Build logs")

    assert set_title(tmp_path, "terminal:build", "   ") is None

    assert read_titles(tmp_path) == {}


def test_clearing_a_title_leaves_the_others_alone(tmp_path: Path) -> None:
    set_title(tmp_path, "terminal:build", "Build logs")
    set_title(tmp_path, "chat:agent-7", "Planning")

    assert set_title(tmp_path, "terminal:build", "") is None

    assert read_titles(tmp_path) == {"chat:agent-7": "Planning"}


def test_clearing_a_ref_that_was_never_named_is_a_noop(tmp_path: Path) -> None:
    assert set_title(tmp_path, "terminal:build", "") is None
    assert clear_title(tmp_path, "terminal:build") is False
    assert read_titles(tmp_path) == {}
    assert not (tmp_path / _TITLES_FILENAME).exists()


def test_clear_title_drops_the_name_of_a_destroyed_object(tmp_path: Path) -> None:
    # Destroy is what unfiles a name: refs are handed out again -- the terminal
    # allocator reuses the lowest free ``terminal-<N>`` -- so a name left behind
    # would land on whatever answers to that ref next.
    set_title(tmp_path, "terminal:terminal-4", "Build logs")

    assert clear_title(tmp_path, "terminal:terminal-4") is True

    assert read_titles(tmp_path) == {}
    assert set_title(tmp_path, "terminal:terminal-4", "Something else") == "Something else"


def test_a_ref_no_object_answers_to_can_still_be_named(tmp_path: Path) -> None:
    # Nothing here checks a ref against the machine: naming is ordinary for an
    # object filed in no project, and Everything is where those show up.
    assert set_title(tmp_path, "chat:agent-nowhere", "Scratch") == "Scratch"
    assert read_titles(tmp_path)["chat:agent-nowhere"] == "Scratch"


def test_a_blank_ref_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectMemberRefError):
        set_title(tmp_path, "   ", "Docs")
    with pytest.raises(ProjectMemberRefError):
        clear_title(tmp_path, "")


def test_a_title_at_the_cap_is_kept_and_a_longer_one_is_rejected(tmp_path: Path) -> None:
    at_cap = "n" * MAX_MEMBER_TITLE_LENGTH
    assert set_title(tmp_path, "terminal:build", at_cap) == at_cap

    with pytest.raises(MemberTitleLengthError):
        set_title(tmp_path, "terminal:build", "n" * (MAX_MEMBER_TITLE_LENGTH + 1))

    # The rejected name changed nothing.
    assert read_titles(tmp_path) == {"terminal:build": at_cap}


def test_a_title_is_measured_after_trimming(tmp_path: Path) -> None:
    padded = f"  {'n' * MAX_MEMBER_TITLE_LENGTH}  "
    assert set_title(tmp_path, "terminal:build", padded) == "n" * MAX_MEMBER_TITLE_LENGTH


def test_corrupt_titles_read_as_unnamed(tmp_path: Path) -> None:
    set_title(tmp_path, "terminal:build", "Build logs")
    (tmp_path / _TITLES_FILENAME).write_text("garbage{")

    assert read_titles(tmp_path) == {}

    # And the store is usable again from there rather than stuck.
    assert set_title(tmp_path, "terminal:build", "Build logs") == "Build logs"
    assert read_titles(tmp_path) == {"terminal:build": "Build logs"}


def test_a_hand_edited_file_keeps_only_the_entries_that_are_names(tmp_path: Path) -> None:
    (tmp_path / _TITLES_FILENAME).write_text('{"title_by_ref": {"terminal:build": 7, "chat:agent-7": "Planning"}}')
    assert read_titles(tmp_path) == {"chat:agent-7": "Planning"}
    (tmp_path / _TITLES_FILENAME).write_text('{"title_by_ref": []}')
    assert read_titles(tmp_path) == {}
