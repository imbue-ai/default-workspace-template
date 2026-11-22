"""Tests for the workspace's tour record."""

import json
from pathlib import Path

import pytest
from onboarding_activity.markers import MARKER_DIR_ENV
from onboarding_activity.markers import Marker
from onboarding_activity.markers import is_marked
from onboarding_activity.markers import marked_at

from imbue.system_interface.shell.tour import OFFER_TOUR_FILENAME
from imbue.system_interface.shell.tour import TOUR_FILENAME
from imbue.system_interface.shell.tour import TourProgress
from imbue.system_interface.shell.tour import TourStore
from imbue.system_interface.shell.tour import UNOFFERED_TOUR
from imbue.system_interface.shell.tour import UNSEEN_TOUR


def test_a_new_workspaces_tour_record_defaults_to_unseen_and_round_trips(tmp_path: Path) -> None:
    (tmp_path / OFFER_TOUR_FILENAME).touch()
    store = TourStore(state_directory=tmp_path)
    assert store.read() == UNSEEN_TOUR
    store.write(TourProgress(is_seen=True, resume_index=4))
    assert store.read() == TourProgress(is_seen=True, resume_index=4)
    assert json.loads((tmp_path / TOUR_FILENAME).read_text()) == {
        "version": 1,
        "is_seen": True,
        "resume_index": 4,
        "is_finished": False,
    }
    store.write(TourProgress(is_seen=True, resume_index=None))
    assert store.read() == TourProgress(is_seen=True, resume_index=None)


def test_a_record_written_before_the_resume_index_reads_with_nothing_to_resume(tmp_path: Path) -> None:
    store = TourStore(state_directory=tmp_path)
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 1, "is_seen": True}))
    assert store.read() == TourProgress(is_seen=True, resume_index=None)


def test_a_workspace_not_marked_new_reads_as_having_seen_the_tour_unless_a_record_says_otherwise(
    tmp_path: Path,
) -> None:
    store = TourStore(state_directory=tmp_path)
    assert store.read() == UNOFFERED_TOUR
    (tmp_path / TOUR_FILENAME).write_text("{not json")
    assert store.read() == UNOFFERED_TOUR
    # A stored record wins over the default, even one that asks for the tour again.
    store.write(TourProgress(is_seen=False, resume_index=None))
    assert store.read() == UNSEEN_TOUR


def test_an_unreadable_or_foreign_tour_record_reads_as_unseen_in_a_new_workspace(tmp_path: Path) -> None:
    (tmp_path / OFFER_TOUR_FILENAME).touch()
    store = TourStore(state_directory=tmp_path)
    (tmp_path / TOUR_FILENAME).write_text("{not json")
    assert store.read() == UNSEEN_TOUR
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 2, "is_seen": True}))
    assert store.read() == UNSEEN_TOUR
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 1, "is_seen": "not a flag"}))
    assert store.read() == UNSEEN_TOUR
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 1, "is_seen": True, "resume_index": -1}))
    assert store.read() == UNSEEN_TOUR


def test_a_record_written_before_the_finished_flag_reads_as_not_finished(tmp_path: Path) -> None:
    store = TourStore(state_directory=tmp_path)
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 1, "is_seen": True, "resume_index": None}))
    assert store.read() == TourProgress(is_seen=True, resume_index=None, is_finished=False)
    assert not UNSEEN_TOUR.is_finished and not UNOFFERED_TOUR.is_finished


def test_a_finished_record_round_trips_and_sets_the_marker_on_every_such_write(tmp_path: Path) -> None:
    store = TourStore(state_directory=tmp_path)

    store.write(TourProgress(is_seen=True, resume_index=2))
    store.write(TourProgress(is_seen=True, resume_index=None))

    # Seen, and even seen with nothing to resume, is not finished until the page says so.
    assert not is_marked(Marker.TOUR_FINISHED)

    store.write(TourProgress(is_seen=True, resume_index=None, is_finished=True))

    assert store.read() == TourProgress(is_seen=True, resume_index=None, is_finished=True)
    assert json.loads((tmp_path / TOUR_FILENAME).read_text())["is_finished"] is True
    first_marked_at = marked_at(Marker.TOUR_FINISHED)
    assert first_marked_at is not None

    store.write(TourProgress(is_seen=True, resume_index=None, is_finished=True))

    second_marked_at = marked_at(Marker.TOUR_FINISHED)
    assert second_marked_at is not None and second_marked_at >= first_marked_at


def test_the_marker_outlives_a_record_that_asks_for_the_tour_again(tmp_path: Path) -> None:
    store = TourStore(state_directory=tmp_path)
    store.write(TourProgress(is_seen=True, resume_index=None, is_finished=True))

    store.write(TourProgress(is_seen=False, resume_index=None))

    assert store.read() == UNSEEN_TOUR
    assert is_marked(Marker.TOUR_FINISHED)


def test_a_marker_that_cannot_be_written_does_not_fail_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file where the marker directory should be: nothing can be made under it.
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    monkeypatch.setenv(MARKER_DIR_ENV, str(blocked))
    store = TourStore(state_directory=tmp_path / "state")

    store.write(TourProgress(is_seen=True, resume_index=None, is_finished=True))

    assert store.read() == TourProgress(is_seen=True, resume_index=None, is_finished=True)
