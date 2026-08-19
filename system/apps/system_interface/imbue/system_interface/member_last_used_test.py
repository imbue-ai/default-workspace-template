import json
import time
from pathlib import Path

import pytest

from imbue.system_interface.member_last_used import MemberLastUsedTimestampError
from imbue.system_interface.member_last_used import clear_last_used
from imbue.system_interface.member_last_used import read_last_used
from imbue.system_interface.member_last_used import touch_last_used
from imbue.system_interface.projects import ProjectMemberRefError

_LAST_USED_FILENAME = "member_last_used.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def test_an_untouched_workspace_has_no_recency_and_no_file(tmp_path: Path) -> None:
    assert read_last_used(tmp_path) == {}
    assert not (tmp_path / _LAST_USED_FILENAME).exists()


def test_touch_last_used_round_trips(tmp_path: Path) -> None:
    at_ms = _now_ms() - 1_000
    assert touch_last_used(tmp_path, "service:docs-viewer", at_ms) == at_ms
    assert read_last_used(tmp_path) == {"service:docs-viewer": at_ms}
    assert (tmp_path / _LAST_USED_FILENAME).exists()


def test_recency_survives_a_round_trip_through_the_file(tmp_path: Path) -> None:
    # The map is the machine's, so it has to outlive the process that wrote it:
    # every read goes back to the file rather than to anything held in memory.
    base_ms = _now_ms() - 10_000
    touch_last_used(tmp_path, "chat:agent-7", base_ms)
    touch_last_used(tmp_path, "terminal:build", base_ms + 1)
    touch_last_used(tmp_path, "service:browser?session=2", base_ms + 2)

    assert read_last_used(tmp_path) == {
        "chat:agent-7": base_ms,
        "terminal:build": base_ms + 1,
        "service:browser?session=2": base_ms + 2,
    }


def test_recency_is_stored_by_ref_and_nowhere_near_a_project(tmp_path: Path) -> None:
    # Recency is keyed by the object and by nothing else, which is the whole
    # point: the object used in one view ranks the same in every other one, and
    # the projects registry is not even created by touching something.
    at_ms = _now_ms() - 1_000
    touch_last_used(tmp_path, "service:docs-viewer", at_ms)

    assert json.loads((tmp_path / _LAST_USED_FILENAME).read_text()) == {
        "last_used_ms_by_ref": {"service:docs-viewer": at_ms}
    }
    assert not (tmp_path / "projects_meta.json").exists()


def test_a_later_touch_moves_the_recency_forward(tmp_path: Path) -> None:
    earlier_ms = _now_ms() - 10_000
    touch_last_used(tmp_path, "terminal:build", earlier_ms)
    assert touch_last_used(tmp_path, "terminal:build", earlier_ms + 5_000) == earlier_ms + 5_000
    assert read_last_used(tmp_path) == {"terminal:build": earlier_ms + 5_000}


def test_a_touch_never_moves_the_recency_backwards(tmp_path: Path) -> None:
    # Two clients racing may land their touches out of order; the later moment
    # wins regardless of arrival order, so the launcher never demotes what was
    # in front of the user a second ago.
    later_ms = _now_ms() - 1_000
    touch_last_used(tmp_path, "terminal:build", later_ms)

    assert touch_last_used(tmp_path, "terminal:build", later_ms - 5_000) == later_ms

    assert read_last_used(tmp_path) == {"terminal:build": later_ms}


def test_a_touch_a_little_ahead_of_the_clock_is_clamped_to_now(tmp_path: Path) -> None:
    # Two clocks disagreeing by a little is ordinary; "used in the future" is
    # not an answer the launcher should ever be handed.
    before_ms = _now_ms()
    stored_ms = touch_last_used(tmp_path, "terminal:build", before_ms + 30_000)

    assert before_ms <= stored_ms <= _now_ms()
    assert read_last_used(tmp_path) == {"terminal:build": stored_ms}


def test_a_non_positive_or_absurd_timestamp_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MemberLastUsedTimestampError):
        touch_last_used(tmp_path, "terminal:build", 0)
    with pytest.raises(MemberLastUsedTimestampError):
        touch_last_used(tmp_path, "terminal:build", -5)
    # More than a day ahead is not clock skew; nothing here was used then.
    with pytest.raises(MemberLastUsedTimestampError):
        touch_last_used(tmp_path, "terminal:build", _now_ms() + 48 * 60 * 60 * 1000)

    # The rejected touches changed nothing.
    assert read_last_used(tmp_path) == {}
    assert not (tmp_path / _LAST_USED_FILENAME).exists()


def test_clearing_a_ref_that_was_never_used_is_a_noop(tmp_path: Path) -> None:
    assert clear_last_used(tmp_path, "terminal:build") is False
    assert read_last_used(tmp_path) == {}
    assert not (tmp_path / _LAST_USED_FILENAME).exists()


def test_clear_last_used_drops_the_recency_of_a_destroyed_object(tmp_path: Path) -> None:
    # Destroy is what unfiles a recency: refs are handed out again -- the
    # terminal allocator reuses the lowest free ``terminal-<N>`` -- so a
    # timestamp left behind would rank whatever answers to that ref next as
    # recently used the moment it appears.
    touch_last_used(tmp_path, "terminal:terminal-4", _now_ms() - 1_000)

    assert clear_last_used(tmp_path, "terminal:terminal-4") is True

    assert read_last_used(tmp_path) == {}


def test_clearing_a_recency_leaves_the_others_alone(tmp_path: Path) -> None:
    base_ms = _now_ms() - 10_000
    touch_last_used(tmp_path, "terminal:build", base_ms)
    touch_last_used(tmp_path, "chat:agent-7", base_ms + 1)

    assert clear_last_used(tmp_path, "terminal:build") is True

    assert read_last_used(tmp_path) == {"chat:agent-7": base_ms + 1}


def test_a_ref_no_object_answers_to_can_still_be_touched(tmp_path: Path) -> None:
    # Nothing here checks a ref against the machine: an object filed in no
    # project is ordinary, and Everything is where those show up.
    at_ms = _now_ms() - 1_000
    assert touch_last_used(tmp_path, "chat:agent-nowhere", at_ms) == at_ms
    assert read_last_used(tmp_path)["chat:agent-nowhere"] == at_ms


def test_a_blank_ref_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProjectMemberRefError):
        touch_last_used(tmp_path, "   ", _now_ms())
    with pytest.raises(ProjectMemberRefError):
        clear_last_used(tmp_path, "")


def test_a_ref_is_trimmed(tmp_path: Path) -> None:
    at_ms = _now_ms() - 1_000
    assert touch_last_used(tmp_path, "  terminal:build  ", at_ms) == at_ms
    assert read_last_used(tmp_path) == {"terminal:build": at_ms}


def test_a_corrupt_file_reads_as_nothing_used(tmp_path: Path) -> None:
    at_ms = _now_ms() - 1_000
    touch_last_used(tmp_path, "terminal:build", at_ms)
    (tmp_path / _LAST_USED_FILENAME).write_text("garbage{")

    assert read_last_used(tmp_path) == {}

    # And the store is usable again from there rather than stuck.
    assert touch_last_used(tmp_path, "terminal:build", at_ms) == at_ms
    assert read_last_used(tmp_path) == {"terminal:build": at_ms}


def test_a_hand_edited_file_keeps_only_the_entries_that_are_timestamps(tmp_path: Path) -> None:
    (tmp_path / _LAST_USED_FILENAME).write_text(
        '{"last_used_ms_by_ref": {"terminal:build": "yesterday", "chat:agent-7": 1700000000000,'
        ' "service:web": -3, "terminal:deploy": true}}'
    )
    assert read_last_used(tmp_path) == {"chat:agent-7": 1700000000000}
    (tmp_path / _LAST_USED_FILENAME).write_text('{"last_used_ms_by_ref": []}')
    assert read_last_used(tmp_path) == {}
