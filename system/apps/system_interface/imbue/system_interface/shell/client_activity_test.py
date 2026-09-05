from pathlib import Path

from imbue.system_interface.shell.client_activity import ClientActivityLog
from imbue.system_interface.shell.client_activity import MESSAGE_TEXT_TRUNCATION_LIMIT
from imbue.system_interface.shell.client_activity import RECENT_MESSAGES_PER_CLIENT
from imbue.system_interface.shell.client_activity import find_client_id_for_instance
from imbue.system_interface.shell.client_activity import summarize_client_activity


def _log(tmp_path: Path) -> ClientActivityLog:
    return ClientActivityLog(events_path=tmp_path / "events" / "client_activity" / "events.jsonl")


def _connected(client_id: str, active_view: str, device_kind: str) -> dict[str, str]:
    """A live registration as the broadcaster reports it."""
    return {"client_id": client_id, "active_view": active_view, "device_kind": device_kind}


def test_messages_are_appended_truncated_and_read_back_in_order(tmp_path: Path) -> None:
    log = _log(tmp_path)
    assert log.read_events() == []
    log.append_message("c1", "desktop", "alpha", "chat", "agent-1", "x" * (MESSAGE_TEXT_TRUNCATION_LIMIT + 5))
    log.append_view_switch("c1", "desktop", "alpha", "everything")
    events = log.read_events()
    assert [event["type"] for event in events] == ["message", "view_switch"]
    assert len(events[0]["text"]) == MESSAGE_TEXT_TRUNCATION_LIMIT
    assert events[0]["is_text_truncated"] is True
    assert events[0]["app"] == "chat" and events[0]["key"] == "agent-1" and events[0]["view_id"] == "alpha"
    assert events[1]["from_view_id"] == "alpha" and events[1]["to_view_id"] == "everything"


def test_the_summary_folds_the_log_per_client(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for index in range(RECENT_MESSAGES_PER_CLIENT + 2):
        log.append_message("c1", "desktop", "alpha", "chat", "agent-1", f"m{index}")
    log.append_view_switch("c1", "desktop", "alpha", "everything")
    log.append_message("c2", "mobile", "beta", "chat", "agent-2", "hello")
    summaries = summarize_client_activity(log.read_events(), [_connected("c2", "gamma", "mobile")])
    assert [summary["client_id"] for summary in summaries] == ["c2", "c1"]
    first, second = summaries[1], summaries[0]
    assert first["active_view"] == "everything"
    assert first["is_connected"] is False
    assert [message["text"] for message in first["recent_messages"]] == [
        f"m{index}" for index in range(2, RECENT_MESSAGES_PER_CLIENT + 2)
    ]
    assert first["recent_messages"][0]["address"] == "app:chat?instance=agent-1"
    # The live registration outranks the log for the view a connected client is on.
    assert second["is_connected"] is True and second["device_kind"] == "mobile"
    assert second["active_view"] == "gamma"


def test_a_connected_client_with_no_activity_is_still_listed(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "desktop", "alpha", "chat", "agent-1", "hello")
    summaries = summarize_client_activity(log.read_events(), [_connected("c9", "beta", "desktop")])
    assert [summary["client_id"] for summary in summaries] == ["c1", "c9"]
    silent = summaries[1]
    assert silent["is_connected"] is True
    assert silent["active_view"] == "beta"
    assert silent["device_kind"] == "desktop"
    assert silent["recent_messages"] == [] and silent["last_seen"] == ""


def test_a_message_to_a_single_instance_app_is_summarized_with_the_bare_address(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "desktop", "alpha", "files", "", "open the notes")
    (summary,) = summarize_client_activity(log.read_events(), [])
    assert summary["recent_messages"][0]["address"] == "app:files"


def test_the_last_client_to_message_an_instance_is_found(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "desktop", "alpha", "chat", "agent-1", "one")
    log.append_message("c2", "desktop", "alpha", "chat", "agent-1", "two")
    log.append_message("c3", "desktop", "alpha", "chat", "agent-2", "three")
    events = log.read_events()
    assert find_client_id_for_instance(events, "chat", "agent-1") == "c2"
    assert find_client_id_for_instance(events, "chat", "agent-9") is None
    assert find_client_id_for_instance(events, "chat", "") is None


def test_unparsable_lines_are_skipped(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message("c1", "desktop", "alpha", "chat", "agent-1", "one")
    with log.events_path.open("a") as event_file:
        event_file.write("not json\n")
    assert len(log.read_events()) == 1
