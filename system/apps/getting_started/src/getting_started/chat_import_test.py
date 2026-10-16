"""Tests for the chat import card's state: reading the skill's status file and keeping the card's dismissal."""

import json
import os
import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from getting_started.chat_import import ChatImportStore
from getting_started.chat_import import HEARTBEAT_STALE_AFTER
from getting_started.chat_import import is_heartbeat_stale
from getting_started.chat_import import is_process_alive
from getting_started.chat_import import read_chat_import_sources


def _write_status(path: Path, sources: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sources": sources}))


def _exited_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def test_no_status_file_means_nothing_imported_yet(tmp_path: Path) -> None:
    assert read_chat_import_sources(tmp_path / "status.json") == {}


def test_each_recorded_source_is_read_and_one_of_another_shape_is_skipped(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(
        status_path,
        {
            "claude": {"state": "imported", "conversations": 40, "updated_at": "t", "detail": "", "pid": None},
            "chatgpt": {"state": "needs_sign_in", "conversations": 0, "detail": "HTTP 401"},
            "mystery": {"state": "dancing"},
        },
    )

    sources = read_chat_import_sources(status_path)

    assert sorted(sources) == ["chatgpt", "claude"]
    assert (sources["claude"].state, sources["claude"].conversations) == ("imported", 40)
    assert (sources["chatgpt"].state, sources["chatgpt"].detail) == ("needs_sign_in", "HTTP 401")


def test_a_status_file_without_a_sources_object_reads_as_nothing_imported(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"sources": []}))

    assert read_chat_import_sources(status_path) == {}


def test_an_import_whose_process_is_still_running_stays_importing(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(status_path, {"claude": {"state": "importing", "conversations": 3, "pid": os.getpid()}})

    assert read_chat_import_sources(status_path)["claude"].state == "importing"


def test_an_import_whose_process_is_gone_reads_as_failed(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(
        status_path,
        {
            "claude": {"state": "importing", "conversations": 3, "pid": _exited_pid()},
            "chatgpt": {"state": "importing", "conversations": 0, "pid": None, "fetched": 166, "to_fetch": 582},
        },
    )

    sources = read_chat_import_sources(status_path)

    assert {key: source.state for key, source in sources.items()} == {"claude": "failed", "chatgpt": "failed"}
    assert sources["claude"].detail == "The import stopped before it finished."
    assert sources["claude"].conversations == 3
    assert (sources["chatgpt"].fetched, sources["chatgpt"].to_fetch) == (None, None)


def test_process_liveness() -> None:
    assert is_process_alive(os.getpid()) is True
    assert is_process_alive(_exited_pid()) is False


def test_the_card_stays_put_away_once_dismissed(tmp_path: Path) -> None:
    store = ChatImportStore(
        status_path=tmp_path / "status.json", dismissal_path=tmp_path / "state" / "chat_import.json"
    )
    assert store.is_dismissed() is False

    store.dismiss()

    assert store.is_dismissed() is True
    assert ChatImportStore(status_path=store.status_path, dismissal_path=store.dismissal_path).wire_json() == {
        "is_dismissed": True,
        "sources": {},
    }


def test_a_running_imports_fetch_progress_reaches_the_card(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(
        status_path,
        {"chatgpt": {"state": "importing", "conversations": 40, "pid": os.getpid(), "fetched": 166, "to_fetch": 582}},
    )

    wire = ChatImportStore(status_path=status_path, dismissal_path=tmp_path / "chat_import.json").wire_json()

    assert (wire["sources"]["chatgpt"]["fetched"], wire["sources"]["chatgpt"]["to_fetch"]) == (166, 582)


def test_an_import_whose_record_stopped_being_rewritten_reads_as_failed_even_with_a_live_pid(tmp_path: Path) -> None:
    status_path = tmp_path / "status.json"
    _write_status(
        status_path,
        {
            "stale": {
                "state": "importing",
                "conversations": 3,
                "pid": os.getpid(),
                "updated_at": "2026-10-08T16:00:00+00:00",
            },
            "fresh": {
                "state": "importing",
                "conversations": 3,
                "pid": os.getpid(),
                "updated_at": "2026-10-08T16:04:30+00:00",
            },
            "untimed": {"state": "importing", "conversations": 3, "pid": os.getpid(), "updated_at": ""},
        },
    )

    sources = read_chat_import_sources(status_path, now=datetime(2026, 10, 8, 16, 5, tzinfo=timezone.utc))

    assert {key: source.state for key, source in sources.items()} == {
        "stale": "failed",
        "fresh": "importing",
        "untimed": "importing",
    }


def test_a_heartbeat_is_stale_only_past_the_threshold() -> None:
    now = datetime(2026, 10, 8, 16, 5, tzinfo=timezone.utc)

    assert is_heartbeat_stale((now - HEARTBEAT_STALE_AFTER).isoformat(), now) is False
    assert is_heartbeat_stale((now - HEARTBEAT_STALE_AFTER - timedelta(seconds=1)).isoformat(), now) is True
    assert is_heartbeat_stale("2026-10-08T16:04:59", now) is False
    assert is_heartbeat_stale("not a time", now) is False
