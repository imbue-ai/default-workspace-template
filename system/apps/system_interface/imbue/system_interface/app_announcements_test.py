"""Tests for the services event stream the shell writes for the minds desktop."""

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import RegistryRow
from app_manifest.registry import read_registry

from imbue.imbue_common.logging import ROTATED_JSONL_PATTERN
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.app_announcements import ANNOUNCEMENTS_ARCHIVE_THRESHOLD_BYTES
from imbue.system_interface.app_announcements import ANNOUNCEMENTS_REL
from imbue.system_interface.app_announcements import AppAnnouncementWriter
from imbue.system_interface.app_announcements import announced_row_of
from imbue.system_interface.app_announcements import announcements_path_from_environment
from imbue.system_interface.app_announcements import compress_staged_announcements
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry


def _rows(tmp_path: Path, *row_toml: str) -> list[RegistryRow]:
    return read_registry(write_registry(tmp_path / "apps.toml", *row_toml))


def _events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line]


def _registered(
    name: str,
    url: str,
    label: str = "",
    display_name: str | None = None,
    is_internal: bool = False,
    is_shareable: bool = True,
) -> str:
    return registry_row_toml(
        name, url, label=label, display_name=display_name, is_internal=is_internal, is_shareable=is_shareable
    )


def test_the_first_announcement_registers_every_app(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    """A consumer reading from the start of the stream needs the whole set, so the first read (nothing
    remembered) announces every app."""

    announcement_writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:8300"))
    )

    events = _events(announcement_writer.events_path)
    assert [(event["type"], event["service"]) for event in events] == [
        ("service_registered", "chat"),
        ("service_registered", "files"),
    ]
    assert events[0]["source"] == "services" and events[0]["event_id"].startswith("evt-")
    assert events[0]["url"] == "http://localhost:8010"


def test_an_unchanged_registry_announces_nothing(announcement_writer: AppAnnouncementWriter, tmp_path: Path) -> None:
    """The guard against the event flood: ``forward_port.py`` rewrites the whole registry whenever any app
    registers, so a restarting app must not re-announce every app in the file."""
    rows = _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:8300"))
    announcement_writer.announce(rows)

    for _ in range(5):
        announcement_writer.announce(rows)

    assert len(_events(announcement_writer.events_path)) == 2


def test_only_the_app_whose_row_changed_is_re_announced(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    announcement_writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:8300"))
    )

    announcement_writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("files", "http://localhost:9999"))
    )

    events = _events(announcement_writer.events_path)
    assert [(event["type"], event["service"]) for event in events[2:]] == [("service_registered", "files")]
    assert events[2]["url"] == "http://localhost:9999"


def test_a_new_app_registers_and_a_removed_one_deregisters(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    announcement_writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("old", "http://localhost:8200"))
    )

    announcement_writer.announce(
        _rows(tmp_path, _registered("chat", "http://localhost:8010"), _registered("new", "http://localhost:8400"))
    )

    assert [(event["type"], event["service"]) for event in _events(announcement_writer.events_path)[2:]] == [
        ("service_registered", "new"),
        ("service_deregistered", "old"),
    ]


def test_a_relabelled_app_is_re_announced(announcement_writer: AppAnnouncementWriter, tmp_path: Path) -> None:
    """The label is the origin consumers route on, so a change to it must reach them even though the app's
    name and URL are untouched."""
    announcement_writer.announce(_rows(tmp_path, _registered("chat", "http://localhost:8010", label="chat-aaaa1111")))

    announcement_writer.announce(_rows(tmp_path, _registered("chat", "http://localhost:8010", label="chat-bbbb2222")))

    events = _events(announcement_writer.events_path)
    assert [(event["type"], event["service"]) for event in events[1:]] == [("service_registered", "chat")]
    assert events[1]["label"] == "chat-bbbb2222"


def test_an_app_is_announced_shareable_unless_it_opts_out_or_is_internal(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    """The minds Share tab offers exactly the apps announced shareable, so an internal app (the terminal's pty) must
    be announced unshareable even though its row never says so."""
    announcement_writer.announce(
        _rows(
            tmp_path,
            _registered("files", "http://localhost:8300"),
            _registered("terminal", "http://localhost:7681", is_shareable=False),
            _registered("terminal-pty", "http://localhost:7683", is_internal=True),
        )
    )

    assert {event["service"]: event["shareable"] for event in _events(announcement_writer.events_path)} == {
        "files": True,
        "terminal": False,
        "terminal-pty": False,
    }


def test_an_app_whose_shareability_changed_is_re_announced(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    announcement_writer.announce(_rows(tmp_path, _registered("files", "http://localhost:8300")))

    announcement_writer.announce(_rows(tmp_path, _registered("files", "http://localhost:8300", is_shareable=False)))

    events = _events(announcement_writer.events_path)
    assert [(event["service"], event["shareable"]) for event in events] == [("files", True), ("files", False)]


def test_an_announcement_carries_the_apps_display_name(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    """The stream is the only way the name users read leaves the workspace, and the minds desktop's share panel
    has nothing but the registered name to show without it."""
    announcement_writer.announce(
        _rows(tmp_path, _registered("files", "http://localhost:8300", display_name="File Viewer"))
    )

    assert _events(announcement_writer.events_path)[0]["display_name"] == "File Viewer"


def test_a_renamed_app_is_re_announced(announcement_writer: AppAnnouncementWriter, tmp_path: Path) -> None:
    """An upgrade can change a manifest's display name alone, and consumers show it, so it must reach them
    even though the app's name, URL, and label are untouched."""
    announcement_writer.announce(_rows(tmp_path, _registered("files", "http://localhost:8300", display_name="Files")))

    announcement_writer.announce(
        _rows(tmp_path, _registered("files", "http://localhost:8300", display_name="File Viewer"))
    )

    events = _events(announcement_writer.events_path)
    assert [(event["type"], event["service"]) for event in events[1:]] == [("service_registered", "files")]
    assert events[1]["display_name"] == "File Viewer"


def test_a_row_with_no_manifest_announces_an_empty_display_name() -> None:
    """``--name --url`` rows (owner-exec, the vm exec service, previews) carry no display name at all, so it
    announces as empty rather than absent: a consumer never has to tell "no name" from "older workspace"."""
    row = RegistryRow(name=AppName("owner-exec"), url=AppUrl("http://localhost:8700"))

    assert announced_row_of(row).display_name == ""


def test_an_unwritable_stream_is_announced_again_on_the_next_read(tmp_path: Path) -> None:
    """A write that fails leaves nothing remembered, so the rows are not lost to the consumer."""
    blocked = tmp_path / "events"
    blocked.write_text("a file where the directory should be")
    writer = AppAnnouncementWriter(events_path=blocked / "services" / "events.jsonl")
    rows = _rows(tmp_path, _registered("chat", "http://localhost:8010"))

    writer.announce(rows)
    blocked.unlink()
    writer.announce(rows)

    assert [event["service"] for event in _events(blocked / "services" / "events.jsonl")] == ["chat"]


def test_the_stream_path_comes_from_the_agent_state_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    assert announcements_path_from_environment() == tmp_path / ANNOUNCEMENTS_REL
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR")
    assert announcements_path_from_environment() is None


def _legacy_stream_over_the_archive_threshold() -> bytes:
    """A stream shaped like one the retired app watcher grew: one app re-announced over and over."""
    line = (
        json.dumps(
            {
                "timestamp": "2026-09-01T00:00:00.000000000Z",
                "type": "service_registered",
                "event_id": "evt-legacy",
                "source": "services",
                "service": "relationships",
                "url": "http://localhost:8083",
            }
        )
        + "\n"
    ).encode()
    return line * (ANNOUNCEMENTS_ARCHIVE_THRESHOLD_BYTES // len(line) + 1)


def _stream_neighbours(announcement_writer: AppAnnouncementWriter) -> list[Path]:
    events_path = announcement_writer.events_path
    return sorted(events_path.parent.glob(f"{events_path.name}.*"))


def test_an_oversized_stream_is_archived_out_of_the_replay_and_restarted_fresh(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    """Consumers replay the whole stream each time they attach, so a stream past the threshold when the shell
    starts is moved to a compressed archive they do not replay, and the first announcement starts a new stream
    holding only the current apps."""
    legacy = _legacy_stream_over_the_archive_threshold()
    announcement_writer.events_path.write_bytes(legacy)

    announcement_writer.announce(_rows(tmp_path, _registered("relationships", "http://localhost:8085")))

    assert [(event["type"], event["service"], event["url"]) for event in _events(announcement_writer.events_path)] == [
        ("service_registered", "relationships", "http://localhost:8085")
    ]
    wait_for(
        lambda: [path.suffix for path in _stream_neighbours(announcement_writer)] == [".gz"],
        timeout=5.0,
        error_message="the staged stream was never compressed",
    )
    (archive,) = _stream_neighbours(announcement_writer)
    assert gzip.decompress(archive.read_bytes()) == legacy
    # mngr replays rotated files named ``events.jsonl.<digits>``; the archive is not one.
    assert ROTATED_JSONL_PATTERN.match(archive.name) is None


def test_a_stream_under_the_threshold_is_left_in_place(
    announcement_writer: AppAnnouncementWriter, tmp_path: Path
) -> None:
    announcement_writer.announce(_rows(tmp_path, _registered("chat", "http://localhost:8010")))
    before = announcement_writer.events_path.read_bytes()

    restarted_writer = AppAnnouncementWriter(events_path=announcement_writer.events_path)
    restarted_writer.announce(_rows(tmp_path, _registered("chat", "http://localhost:8010")))

    assert announcement_writer.events_path.read_bytes().startswith(before)
    assert _stream_neighbours(announcement_writer) == []


def test_an_interrupted_archive_is_finished_and_only_the_newest_archives_are_kept(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    for month in range(1, 5):
        (tmp_path / f"events.jsonl.20260{month}01000000000000.gz").write_bytes(gzip.compress(b"old\n"))
    # Left behind by a shell stopped after staging a stream but before compressing it.
    (tmp_path / "events.jsonl.20260501000000000000.archiving").write_bytes(b"staged\n")

    compress_staged_announcements(events_path)

    assert sorted(path.name for path in tmp_path.glob("events.jsonl.*")) == [
        "events.jsonl.20260301000000000000.gz",
        "events.jsonl.20260401000000000000.gz",
        "events.jsonl.20260501000000000000.gz",
    ]
    assert gzip.decompress((tmp_path / "events.jsonl.20260501000000000000.gz").read_bytes()) == b"staged\n"
