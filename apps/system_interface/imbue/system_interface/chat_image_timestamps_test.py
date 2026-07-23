import os
from pathlib import Path

from imbue.system_interface.chat_image_timestamps import ChatImageStatus
from imbue.system_interface.chat_image_timestamps import ChatImageTimestampStore
from imbue.system_interface.chat_image_timestamps import extract_image_paths


def _rewind_mtime(path: Path) -> None:
    """Backdate the file's mtime so a same-instant rewrite still reads as changed."""
    stat_result = path.stat()
    os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns - 1_000_000_000))


def test_extract_image_paths_returns_absolute_inline_image_paths() -> None:
    text = (
        "Here is a chart:\n"
        "![Revenue](/mngr/code/runtime/chat-images/revenue.png)\n"
        "and a report [link](/mngr/code/runtime/chat-files/report.pdf)\n"
        "plus an external image ![ext](https://example.com/pic.png)\n"
        "and a relative one ![rel](runtime/chat-images/x.png)"
    )
    assert extract_image_paths(text) == ["/mngr/code/runtime/chat-images/revenue.png"]


def test_extract_image_paths_skips_non_image_extensions() -> None:
    assert extract_image_paths("![f](/tmp/data.csv)") == []


def test_check_records_on_first_sight_and_stays_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatImageStatus.UNCHANGED
    assert store.check("event-1", str(source)) is ChatImageStatus.UNCHANGED


def test_check_reports_changed_after_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatImageStatus.UNCHANGED

    _rewind_mtime(source)
    source.write_bytes(b"changed!")
    assert store.check("event-1", str(source)) is ChatImageStatus.CHANGED


def test_check_reports_changed_after_delete(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatImageStatus.UNCHANGED

    source.unlink()
    assert store.check("event-1", str(source)) is ChatImageStatus.CHANGED


def test_new_event_records_current_content_after_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatImageStatus.UNCHANGED

    _rewind_mtime(source)
    source.write_bytes(b"changed!")
    # A new message referencing the overwritten path records the file as it is
    # now, so the new message renders fine while the old one reports CHANGED.
    assert store.check("event-2", str(source)) is ChatImageStatus.UNCHANGED
    assert store.check("event-1", str(source)) is ChatImageStatus.CHANGED


def test_check_reports_unknown_for_never_seen_missing_file(tmp_path: Path) -> None:
    store = ChatImageTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(tmp_path / "missing.png")) is ChatImageStatus.UNKNOWN


def test_check_reports_unknown_for_non_image_path(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("hello")
    store = ChatImageTimestampStore(tmp_path / "store")
    assert store.check("event-1", str(source)) is ChatImageStatus.UNKNOWN


def test_index_persists_across_store_instances(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store_dir = tmp_path / "store"
    assert ChatImageTimestampStore(store_dir).check("event-1", str(source)) is ChatImageStatus.UNCHANGED

    _rewind_mtime(source)
    source.write_bytes(b"changed!")
    assert ChatImageTimestampStore(store_dir).check("event-1", str(source)) is ChatImageStatus.CHANGED


def test_enqueue_events_records_referenced_images(tmp_path: Path) -> None:
    source = tmp_path / "chart.png"
    source.write_bytes(b"original")
    store = ChatImageTimestampStore(tmp_path / "store")
    event = {
        "event_id": "event-1",
        "type": "assistant_message",
        "text": f"Look: ![chart]({source})",
    }
    store.enqueue_events([event])
    # stop() drains by joining the worker thread after the sentinel, so the
    # queued record is guaranteed to have been attempted once it returns.
    store.stop()

    _rewind_mtime(source)
    source.write_bytes(b"changed!")
    assert store.check("event-1", str(source)) is ChatImageStatus.CHANGED


def test_enqueue_events_ignores_events_without_images(tmp_path: Path) -> None:
    store = ChatImageTimestampStore(tmp_path / "store")
    store.enqueue_events([{"event_id": "event-1", "type": "assistant_message", "text": "no images here"}])
    store.stop()
    assert not (tmp_path / "store").exists()
