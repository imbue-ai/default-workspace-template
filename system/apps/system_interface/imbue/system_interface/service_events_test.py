"""Tests for the services event stream the shell writes for the minds desktop."""

import json
from pathlib import Path
from typing import Any

import pytest
from app_manifest.registry import read_registry

from imbue.system_interface.service_events import SERVICE_EVENTS_REL
from imbue.system_interface.service_events import ServiceEventWriter
from imbue.system_interface.service_events import service_events_path_from_environment
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry


def _rows(tmp_path: Path, *row_toml: str) -> list[Any]:
    return read_registry(write_registry(tmp_path / "apps.toml", *row_toml))


def _events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line]


def _registered(name: str, url: str, label: str = "") -> str:
    return registry_row_toml(name, url, label=label)


def test_the_first_announcement_registers_every_app(tmp_path: Path) -> None:
    """A consumer reading from the start of the stream needs the whole set, so the first read (nothing
    remembered) announces every app."""
    events_path = tmp_path / "events.jsonl"
    writer = ServiceEventWriter(events_path=events_path)

    writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:8300"))
    )

    events = _events(events_path)
    assert [(event["type"], event["service"]) for event in events] == [
        ("service_registered", "chat"),
        ("service_registered", "files"),
    ]
    assert events[0]["source"] == "services" and events[0]["event_id"].startswith("evt-")
    assert events[0]["url"] == "http://localhost:8010"


def test_an_unchanged_registry_announces_nothing(tmp_path: Path) -> None:
    """The guard against the event flood: ``forward_port.py`` rewrites the whole registry whenever any app
    registers, so a restarting app must not re-announce every app in the file."""
    events_path = tmp_path / "events.jsonl"
    writer = ServiceEventWriter(events_path=events_path)
    rows = _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:8300"))
    writer.announce(rows)

    for _ in range(5):
        writer.announce(rows)

    assert len(_events(events_path)) == 2


def test_only_the_app_whose_row_changed_is_re_announced(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    writer = ServiceEventWriter(events_path=events_path)
    writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:8300"))
    )

    writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:9999"))
    )

    events = _events(events_path)
    assert [(event["type"], event["service"]) for event in events[2:]] == [("service_registered", "files")]
    assert events[2]["url"] == "http://localhost:9999"


def test_a_new_app_registers_and_a_removed_one_deregisters(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    writer = ServiceEventWriter(events_path=events_path)
    writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("old", "http://localhost:8200"))
    )

    writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("new", "http://localhost:8400"))
    )

    assert [(event["type"], event["service"]) for event in _events(events_path)[2:]] == [
        ("service_registered", "new"),
        ("service_deregistered", "old"),
    ]


def test_a_relabelled_app_is_re_announced(tmp_path: Path) -> None:
    """The label is the origin consumers route on, so a change to it must reach them even though the app's
    name and URL are untouched."""
    events_path = tmp_path / "events.jsonl"
    writer = ServiceEventWriter(events_path=events_path)
    writer.announce(_rows(tmp_path, _registered("chat", "http://localhost:8010", label="chat-aaaa1111")))

    writer.announce(_rows(tmp_path, _registered("chat", "http://localhost:8010", label="chat-bbbb2222")))

    events = _events(events_path)
    assert [(event["type"], event["service"]) for event in events[1:]] == [("service_registered", "chat")]
    assert events[1]["label"] == "chat-bbbb2222"


def test_an_unwritable_stream_is_announced_again_on_the_next_read(tmp_path: Path) -> None:
    """A write that fails leaves nothing remembered, so the rows are not lost to the consumer."""
    blocked = tmp_path / "events"
    blocked.write_text("a file where the directory should be")
    writer = ServiceEventWriter(events_path=blocked / "services" / "events.jsonl")
    rows = _rows(tmp_path, _registered("chat", "http://localhost:8010"))

    writer.announce(rows)
    blocked.unlink()
    writer.announce(rows)

    assert [event["service"] for event in _events(blocked / "services" / "events.jsonl")] == ["chat"]


def test_the_stream_path_comes_from_the_agent_state_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    assert service_events_path_from_environment() == tmp_path / SERVICE_EVENTS_REL
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR")
    assert service_events_path_from_environment() is None
