import json
from pathlib import Path

from app_watcher import watcher


def _rows(*apps: dict[str, object]) -> dict[str, watcher._AppRow]:
    return watcher._registered_rows(list(apps))


def _app(name: str, url: str, label: str = "", icon: str = "") -> dict[str, object]:
    return {"name": name, "url": url, "label": label, "icon": icon}


def _events(events_dir: Path) -> list[dict[str, object]]:
    path = events_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_the_first_pass_registers_every_app(tmp_path: Path) -> None:
    """A consumer reading from the start of the stream needs the whole set, so the
    first pass (nothing remembered) announces every app."""
    current = _rows(
        _app("chat", "http://localhost:8010"), _app("files", "http://localhost:8300")
    )

    watcher._write_events(tmp_path, current, {})

    assert [(e["type"], e["service"]) for e in _events(tmp_path)] == [
        ("service_registered", "chat"),
        ("service_registered", "files"),
    ]


def test_an_unchanged_registry_emits_nothing(tmp_path: Path) -> None:
    """The regression guard for the event flood: ``forward_port.py`` rewrites the
    whole registry whenever any app registers, so a restarting app used to make the
    watcher re-announce every app in the file, forever."""
    current = _rows(
        _app("chat", "http://localhost:8010"), _app("files", "http://localhost:8300")
    )
    watcher._write_events(tmp_path, current, {})

    for _ in range(5):
        watcher._write_events(tmp_path, current, current)

    assert len(_events(tmp_path)) == 2


def test_only_the_app_whose_row_changed_is_re_announced(tmp_path: Path) -> None:
    previous = _rows(
        _app("chat", "http://localhost:8010"), _app("files", "http://localhost:8300")
    )
    current = _rows(
        _app("chat", "http://localhost:8010"), _app("files", "http://localhost:9999")
    )

    watcher._write_events(tmp_path, current, previous)

    events = _events(tmp_path)
    assert [(e["type"], e["service"]) for e in events] == [
        ("service_registered", "files")
    ]
    assert events[0]["url"] == "http://localhost:9999"


def test_a_new_app_registers_and_a_removed_one_deregisters(tmp_path: Path) -> None:
    previous = _rows(
        _app("chat", "http://localhost:8010"), _app("old", "http://localhost:8200")
    )
    current = _rows(
        _app("chat", "http://localhost:8010"), _app("new", "http://localhost:8400")
    )

    watcher._write_events(tmp_path, current, previous)

    assert [(e["type"], e["service"]) for e in _events(tmp_path)] == [
        ("service_registered", "new"),
        ("service_deregistered", "old"),
    ]


def test_a_relabelled_app_is_re_announced(tmp_path: Path) -> None:
    """The label is the origin consumers route on, so a change to it must reach them
    even though the app's name and URL are untouched."""
    previous = _rows(_app("chat", "http://localhost:8010", label="chat-aaaa1111"))
    current = _rows(_app("chat", "http://localhost:8010", label="chat-bbbb2222"))

    watcher._write_events(tmp_path, current, previous)

    events = _events(tmp_path)
    assert [(e["type"], e["service"]) for e in events] == [
        ("service_registered", "chat")
    ]
    assert events[0]["label"] == "chat-bbbb2222"


def test_an_entry_without_a_url_is_neither_registered_nor_counted_present(
    tmp_path: Path,
) -> None:
    """An unregisterable row must not be emitted, and must not look like a removal
    on the next pass either."""
    assert _rows(_app("half-written", "")) == {}

    previous = _rows(_app("chat", "http://localhost:8010"))
    watcher._write_events(
        tmp_path,
        _rows(_app("chat", "http://localhost:8010"), _app("half-written", "")),
        previous,
    )

    assert _events(tmp_path) == []
