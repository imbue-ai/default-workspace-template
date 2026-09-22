"""Tests for the workspace's avatar selection file."""

import json
from pathlib import Path

from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.avatar.selection import AvatarSelectionStore
from imbue.system_interface.avatar.selection import SELECTION_FILENAME


def test_the_selection_defaults_and_round_trips(tmp_path: Path) -> None:
    store = AvatarSelectionStore(state_directory=tmp_path)
    assert store.read() == DEFAULT_DESIGN_ID
    store.write(DesignId("jelly-cat"))
    assert store.read() == "jelly-cat"
    assert json.loads((tmp_path / SELECTION_FILENAME).read_text()) == {"version": 1, "design": "jelly-cat"}


def test_an_unreadable_or_foreign_selection_reads_as_the_default(tmp_path: Path) -> None:
    store = AvatarSelectionStore(state_directory=tmp_path)
    (tmp_path / SELECTION_FILENAME).write_text("{not json")
    assert store.read() == DEFAULT_DESIGN_ID
    (tmp_path / SELECTION_FILENAME).write_text(json.dumps({"version": 2, "design": "jelly-cat"}))
    assert store.read() == DEFAULT_DESIGN_ID
    (tmp_path / SELECTION_FILENAME).write_text(json.dumps({"version": 1, "design": "Not Valid"}))
    assert store.read() == DEFAULT_DESIGN_ID
