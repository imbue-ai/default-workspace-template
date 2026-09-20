from pathlib import Path

from imbue.system_interface.shell.client_activity import ClientActivityLog
from imbue.system_interface.shell.client_activity import MESSAGE_TEXT_TRUNCATION_LIMIT
from imbue.system_interface.shell.client_activity import RECENT_MESSAGES_PER_CLIENT
from imbue.system_interface.shell.client_activity import find_client_id_for_instance
from imbue.system_interface.shell.client_activity import summarize_client_activity


def _log(tmp_path: Path) -> ClientActivityLog:
    return ClientActivityLog(events_path=tmp_path / "events" / "client_activity" / "events.jsonl")


def _connected(client_id: str, active_desktop: str) -> dict[str, str]:
    """A live registration as the broadcaster reports it (the desktop "" before the client reported one)."""
    return {"client_id": client_id, "active_desktop": active_desktop}


def test_messages_are_appended_truncated_and_read_back_in_order(tmp_path: Path) -> None:
    log = _log(tmp_path)
    assert log.read_events() == []
    log.append_message("c1", "home", "chat", "agent-1", "x" * (MESSAGE_TEXT_TRUNCATION_LIMIT + 5))
    log.append_desktop_switch("c1", "home", "research")
    events = log.read_events()
    assert [event["type"] for event in events] == ["message", "desktop_switch"]
    assert len(events[0]["text"]) == MESSAGE_TEXT_TRUNCATION_LIMIT
    assert events[0]["is_text_truncated"] is True
    assert events[0]["app"] == "chat" and events[0]["key"] == "agent-1" and events[0]["desktop_id"] == "home"
    assert events[1]["from_desktop_id"] == "home" and events[1]["to_desktop_id"] == "research"


def test_the_summary_folds_the_log_per_client(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for index in range(RECENT_MESSAGES_PER_CLIENT + 2):
        log.append_message("c1", "home", "chat", "agent-1", f"m{index}")
    log.append_desktop_switch("c1", "home", "research")
    log.append_message("c2", "home", "chat", "agent-2", "hello")
    summaries = summarize_client_activity(log.read_events(), [_connected("c2", "notes")])
    assert [summary["client_id"] for summary in summaries] == ["c2", "c1"]
    first, second = summaries[1], summaries[0]
    assert first["active_desktop"] == "research"
    assert first["is_connected"] is False
    assert [message["text"] for message in first["recent_messages"]] == [
        f"m{index}" for index in range(2, RECENT_MESSAGES_PER_CLIENT + 2)
    ]
    assert (first["recent_messages"][0]["app"], first["recent_messages"][0]["key"]) == ("chat", "agent-1")
    # The live registration outranks the log for the desktop a connected client is on.
    assert second["is_connected"] is True and second["active_desktop"] == "notes"


def test_a_message_settles_the_desktop_and_an_empty_live_desktop_reads_as_none(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_desktop_switch("c1", "home", "research")
    log.append_message("c1", "notes", "chat", "agent-1", "hello")
    log.append_desktop_switch("c2", "", "home")
    summaries = summarize_client_activity(log.read_events(), [_connected("c2", "")])
    by_id = {summary["client_id"]: summary for summary in summaries}
    assert by_id["c1"]["active_desktop"] == "notes"
    assert by_id["c2"]["active_desktop"] is None and by_id["c2"]["is_connected"] is True


def test_a_connected_client_with_no_activity_is_still_listed(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "home", "chat", "agent-1", "hello")
    summaries = summarize_client_activity(log.read_events(), [_connected("c9", "research")])
    assert [summary["client_id"] for summary in summaries] == ["c1", "c9"]
    silent = summaries[1]
    assert silent["is_connected"] is True
    assert silent["active_desktop"] == "research"
    assert silent["recent_messages"] == [] and silent["last_seen"] == ""


def test_a_message_to_a_page_without_a_marker_is_summarized_with_an_empty_key(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "home", "files", "", "open the notes")
    (summary,) = summarize_client_activity(log.read_events(), [])
    assert (summary["recent_messages"][0]["app"], summary["recent_messages"][0]["key"]) == ("files", "")


def test_the_last_client_to_message_a_page_is_found(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "home", "chat", "agent-1", "one")
    log.append_message("c2", "home", "chat", "agent-1", "two")
    log.append_message("c3", "home", "chat", "agent-2", "three")
    events = log.read_events()
    assert find_client_id_for_instance(events, "chat", "agent-1") == "c2"
    assert find_client_id_for_instance(events, "chat", "agent-9") is None
    assert find_client_id_for_instance(events, "chat", "") is None


def test_unparsable_lines_are_skipped(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "home", "chat", "agent-1", "one")
    with log.events_path.open("a") as event_file:
        event_file.write("not json\n")
    assert len(log.read_events()) == 1
