from pathlib import Path
from typing import Any

from imbue.system_interface.handoff_archive import ArchivedSegmentWatcher
from imbue.system_interface.handoff_archive import CompositeChatWatcher
from imbue.system_interface.handoff_archive import TranscriptArchive
from imbue.system_interface.handoff_archive import archives_dir_for_layout_dir
from imbue.system_interface.handoff_archive import build_chat_watcher
from imbue.system_interface.models import ChatId

_CHAT_ID = ChatId("agent-" + "a" * 32)
_OLD_AGENT_ID = "agent-" + "b" * 32
_NEW_AGENT_ID = "agent-" + "c" * 32


def _event(event_id: str, text: str = "") -> dict[str, Any]:
    return {"event_id": event_id, "type": "assistant_message", "text": text}


def _rows(*event_ids: str) -> list[dict[str, Any]]:
    return [{"event": _event(event_id), "detail": {"payload": event_id}} for event_id in event_ids]


def _segment(agent_id: str, *event_ids: str) -> ArchivedSegmentWatcher:
    watcher = ArchivedSegmentWatcher.build_from_rows(agent_id, _rows(*event_ids))
    watcher.start()
    return watcher


def _event_ids(events: list[dict[str, Any]]) -> list[str]:
    return [event["event_id"] for event in events]


def _archive(tmp_path: Path) -> TranscriptArchive:
    return TranscriptArchive(archives_dir=archives_dir_for_layout_dir(tmp_path))


# -- the archive ------------------------------------------------------------------------


def test_capture_writes_every_event_with_its_detail_inlined(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    source = _segment(_OLD_AGENT_ID, "e1", "e2", "e3")

    count = archive.capture(_CHAT_ID, _OLD_AGENT_ID, source)

    assert count == 3
    rows = archive.load(_CHAT_ID, _OLD_AGENT_ID)
    assert _event_ids([row["event"] for row in rows]) == ["e1", "e2", "e3"]
    # Inlined, not referenced: the agent's transcript files are about to be destroyed.
    assert [row["detail"] for row in rows] == [{"payload": "e1"}, {"payload": "e2"}, {"payload": "e3"}]


def test_a_captured_segment_survives_a_new_archive_over_the_same_dir(tmp_path: Path) -> None:
    _archive(tmp_path).capture(_CHAT_ID, _OLD_AGENT_ID, _segment(_OLD_AGENT_ID, "e1", "e2"))

    reloaded = _archive(tmp_path)

    assert len(reloaded.load(_CHAT_ID, _OLD_AGENT_ID)) == 2


def test_load_of_an_unarchived_segment_is_empty(tmp_path: Path) -> None:
    assert _archive(tmp_path).load(_CHAT_ID, _OLD_AGENT_ID) == []


def test_an_unreadable_row_is_skipped_on_load(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    archive.capture(_CHAT_ID, _OLD_AGENT_ID, _segment(_OLD_AGENT_ID, "e1"))
    path = archives_dir_for_layout_dir(tmp_path) / f"{_CHAT_ID}.{_OLD_AGENT_ID}.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    rows = archive.load(_CHAT_ID, _OLD_AGENT_ID)

    assert _event_ids([row["event"] for row in rows]) == ["e1"]


def test_remove_chat_drops_every_segment_of_that_chat_only(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    other_chat = ChatId("agent-" + "d" * 32)
    archive.capture(_CHAT_ID, _OLD_AGENT_ID, _segment(_OLD_AGENT_ID, "e1"))
    archive.capture(_CHAT_ID, _NEW_AGENT_ID, _segment(_NEW_AGENT_ID, "e2"))
    archive.capture(other_chat, _OLD_AGENT_ID, _segment(_OLD_AGENT_ID, "e3"))

    archive.remove_chat(_CHAT_ID)

    assert archive.load(_CHAT_ID, _OLD_AGENT_ID) == []
    assert archive.load(_CHAT_ID, _NEW_AGENT_ID) == []
    assert len(archive.load(other_chat, _OLD_AGENT_ID)) == 1


def test_an_archive_with_no_dir_keeps_segments_in_memory(tmp_path: Path) -> None:
    archive = TranscriptArchive(archives_dir=None)

    archive.capture(_CHAT_ID, _OLD_AGENT_ID, _segment(_OLD_AGENT_ID, "e1", "e2"))

    assert len(archive.load(_CHAT_ID, _OLD_AGENT_ID)) == 2
    archive.remove_chat(_CHAT_ID)
    assert archive.load(_CHAT_ID, _OLD_AGENT_ID) == []


# -- the archived-segment watcher -------------------------------------------------------


def test_an_archived_segment_serves_its_events_and_inlined_details(tmp_path: Path) -> None:
    watcher = _segment(_OLD_AGENT_ID, "e1", "e2")

    assert _event_ids(watcher.get_all_events()) == ["e1", "e2"]
    assert watcher.get_total_event_count() == 2
    assert watcher.get_event_detail("e2") == {"payload": "e2"}
    assert watcher.get_event_detail("nope") is None


def test_an_archived_row_with_no_event_id_is_skipped(tmp_path: Path) -> None:
    watcher = ArchivedSegmentWatcher.build_from_rows(
        _OLD_AGENT_ID, [{"event": {"type": "assistant_message"}, "detail": None}, *_rows("e1")]
    )
    watcher.start()

    assert _event_ids(watcher.get_all_events()) == ["e1"]


# -- the composite ----------------------------------------------------------------------


def _composite() -> CompositeChatWatcher:
    """One chat: three events on a retired agent, then two on the live one."""
    return CompositeChatWatcher.build_over(
        (_segment(_OLD_AGENT_ID, "a1", "a2", "a3"),), _segment(_NEW_AGENT_ID, "b1", "b2")
    )


def test_the_composite_reads_as_one_continuous_transcript() -> None:
    composite = _composite()

    assert _event_ids(composite.get_all_events()) == ["a1", "a2", "a3", "b1", "b2"]
    assert composite.get_total_event_count() == 5


def test_offsets_are_global_across_segments() -> None:
    composite = _composite()

    assert composite.get_event_offset("a1") == 0
    assert composite.get_event_offset("a3") == 2
    # The live agent's first event sits after the whole retired segment, not at 0.
    assert composite.get_event_offset("b1") == 3
    assert composite.get_event_offset("unknown") == -1


def test_a_window_at_a_global_offset_straddles_the_boundary() -> None:
    composite = _composite()

    assert _event_ids(composite.get_events_at_offset(2, 2)) == ["a3", "b1"]
    assert _event_ids(composite.get_events_at_offset(0, 5)) == ["a1", "a2", "a3", "b1", "b2"]
    assert _event_ids(composite.get_events_at_offset(4, 10)) == ["b2"]
    assert composite.get_events_at_offset(9, 3) == []


def test_the_tail_reaches_back_into_retired_history() -> None:
    composite = _composite()

    # The live agent is younger than the window, which is exactly the first load after a
    # switch: the user must still see the turns that came before it.
    assert _event_ids(composite.get_tail_events(4)) == ["a2", "a3", "b1", "b2"]
    assert _event_ids(composite.get_tail_events(2)) == ["b1", "b2"]
    assert _event_ids(composite.get_tail_events(99)) == ["a1", "a2", "a3", "b1", "b2"]


def test_paging_older_crosses_from_the_live_agent_into_the_archive() -> None:
    composite = _composite()

    # b1 is the live agent's first event, so the whole page comes out of the archive.
    assert _event_ids(composite.get_backfill_events("b1", limit=2)) == ["a2", "a3"]
    # A page that starts inside the live segment is topped up from the archive to `limit`.
    assert _event_ids(composite.get_backfill_events("b2", limit=3)) == ["a2", "a3", "b1"]
    assert _event_ids(composite.get_backfill_events("a2", limit=5)) == ["a1"]
    assert composite.get_backfill_events("unknown", limit=5) == []


def test_paging_newer_crosses_from_the_archive_into_the_live_agent() -> None:
    composite = _composite()

    assert _event_ids(composite.get_forward_events("a2", limit=3)) == ["a3", "b1", "b2"]
    assert _event_ids(composite.get_forward_events("a3", limit=1)) == ["b1"]
    assert composite.get_forward_events("b2", limit=3) == []


def test_details_resolve_from_whichever_segment_holds_the_event() -> None:
    composite = _composite()

    assert composite.get_event_detail("a2") == {"payload": "a2"}
    assert composite.get_event_detail("b2") == {"payload": "b2"}
    assert composite.get_event_detail("unknown") is None


def test_live_only_state_comes_from_the_active_agent() -> None:
    live = _segment(_NEW_AGENT_ID, "b1")
    composite = CompositeChatWatcher.build_over((_segment(_OLD_AGENT_ID, "a1"),), live)

    # The queue, the flush hooks and the SSE filter belong to the running agent alone;
    # a frozen segment has no process to answer for them.
    assert composite.live is live
    assert composite.get_queued_messages() == []


# -- assembly ---------------------------------------------------------------------------


def test_a_chat_that_never_switched_gets_its_live_watcher_unwrapped(tmp_path: Path) -> None:
    live = _segment(_NEW_AGENT_ID, "b1")

    built = build_chat_watcher(_CHAT_ID, (), live, _archive(tmp_path))

    assert built is live


def test_a_switched_chat_gets_a_composite_over_its_archived_segments(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    archive.capture(_CHAT_ID, _OLD_AGENT_ID, _segment(_OLD_AGENT_ID, "a1", "a2"))
    live = _segment(_NEW_AGENT_ID, "b1")

    built = build_chat_watcher(_CHAT_ID, (_OLD_AGENT_ID,), live, archive)

    assert _event_ids(built.get_all_events()) == ["a1", "a2", "b1"]
    assert built.get_event_detail("a1") == {"payload": "a1"}


def test_a_retired_segment_with_no_archive_is_skipped(tmp_path: Path) -> None:
    live = _segment(_NEW_AGENT_ID, "b1")

    # A capture that failed, or a workspace restored without its archives: the chat has
    # lost part of its history and must still serve the rest.
    built = build_chat_watcher(_CHAT_ID, (_OLD_AGENT_ID,), live, _archive(tmp_path))

    assert built is live
