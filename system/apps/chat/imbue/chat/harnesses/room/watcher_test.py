import queue
from pathlib import Path
from typing import Any

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.room.transcript import append_record
from imbue.chat.harnesses.room.transcript import person_step_record
from imbue.chat.harnesses.room.transcript import transcript_path
from imbue.chat.harnesses.room.watcher import RoomSessionWatcher
from imbue.chat.harnesses.room.watcher import RoomTranscriptLoader

_AGENT_ID = "agent-00000000000000000000000000000502"


def _room_agent_info(tmp_path: Path) -> AgentInfo:
    state_dir = tmp_path / "agents" / _AGENT_ID
    state_dir.mkdir(parents=True)
    return AgentInfo(
        id=_AGENT_ID,
        name="Lounge",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=tmp_path / "unused",
        work_dir=str(tmp_path / "work"),
    )


def test_the_loader_reads_the_rooms_messages_in_order_and_skips_the_header(tmp_path: Path) -> None:
    agent_info = _room_agent_info(tmp_path)
    path = transcript_path(agent_info.agent_state_dir)
    append_record(path, _AGENT_ID, person_step_record("first", "m-1"))
    append_record(path, _AGENT_ID, person_step_record("second", "m-2"))

    loader = RoomTranscriptLoader.build_loader(agent_info)

    assert [event["content"] for event in loader.get_all_events()] == ["first", "second"]
    assert loader.get_total_event_count() == 2
    assert [event["agent_id"] for event in loader.get_all_events()] == [_AGENT_ID, _AGENT_ID]


def test_a_loader_over_a_room_with_no_transcript_yet_reads_nothing(tmp_path: Path) -> None:
    loader = RoomTranscriptLoader.build_loader(_room_agent_info(tmp_path))

    assert loader.get_all_events() == []


def test_the_watcher_emits_each_message_appended_after_it_started(tmp_path: Path) -> None:
    agent_info = _room_agent_info(tmp_path)
    path = transcript_path(agent_info.agent_state_dir)
    append_record(path, _AGENT_ID, person_step_record("before", "m-1"))
    emitted: queue.Queue[list[dict[str, Any]]] = queue.Queue()
    watcher = RoomSessionWatcher.build(agent_info, lambda _agent_id, events: emitted.put(events))
    watcher.start()
    try:
        # The backlog is primed, not emitted: the page reads it over REST.
        assert [event["content"] for event in watcher.get_all_events()] == ["before"]
        append_record(path, _AGENT_ID, person_step_record("after", "m-2"))

        events = emitted.get(timeout=10.0)
    finally:
        watcher.stop()
    assert [event["content"] for event in events] == ["after"]
    assert watcher.is_main_session_event(events[0])
