"""Tests for the workspace's tour record."""

import json
from pathlib import Path

from imbue.system_interface.shell.tour import TOUR_FILENAME
from imbue.system_interface.shell.tour import UNSEEN_TOUR
from imbue.system_interface.shell.tour import TourProgress
from imbue.system_interface.shell.tour import TourStore


def test_the_tour_record_defaults_to_unseen_and_round_trips(tmp_path: Path) -> None:
    store = TourStore(state_directory=tmp_path)
    assert store.read() == UNSEEN_TOUR
    store.write(TourProgress(is_seen=True, resume_index=4))
    assert store.read() == TourProgress(is_seen=True, resume_index=4)
    assert json.loads((tmp_path / TOUR_FILENAME).read_text()) == {"version": 1, "is_seen": True, "resume_index": 4}
    store.write(TourProgress(is_seen=True, resume_index=None))
    assert store.read() == TourProgress(is_seen=True, resume_index=None)


def test_a_record_written_before_the_resume_index_reads_with_nothing_to_resume(tmp_path: Path) -> None:
    store = TourStore(state_directory=tmp_path)
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 1, "is_seen": True}))
    assert store.read() == TourProgress(is_seen=True, resume_index=None)


def test_an_unreadable_or_foreign_tour_record_reads_as_unseen(tmp_path: Path) -> None:
    store = TourStore(state_directory=tmp_path)
    (tmp_path / TOUR_FILENAME).write_text("{not json")
    assert store.read() == UNSEEN_TOUR
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 2, "is_seen": True}))
    assert store.read() == UNSEEN_TOUR
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 1, "is_seen": "not a flag"}))
    assert store.read() == UNSEEN_TOUR
    (tmp_path / TOUR_FILENAME).write_text(json.dumps({"version": 1, "is_seen": True, "resume_index": -1}))
    assert store.read() == UNSEEN_TOUR
