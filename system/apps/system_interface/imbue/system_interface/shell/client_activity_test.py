import gzip
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.client_activity import ClientActivityLog
from imbue.system_interface.shell.client_activity import DESKTOP_SWITCH_EVENT_TYPE
from imbue.system_interface.shell.client_activity import KEPT_CLIENT_ACTIVITY_ARCHIVE_COUNT
from imbue.system_interface.shell.client_activity import MESSAGE_TEXT_TRUNCATION_LIMIT
from imbue.system_interface.shell.client_activity import RECENT_MESSAGES_PER_CLIENT
from imbue.system_interface.shell.client_activity import WindowInputKind
from imbue.system_interface.shell.client_activity import find_client_id_for_page
from imbue.system_interface.shell.client_activity import summarize_client_activity
from imbue.system_interface.shell.testing import message_report
from imbue.system_interface.ws_broadcaster import ConnectionRegistration


def _log(tmp_path: Path) -> ClientActivityLog:
    return ClientActivityLog(events_path=tmp_path / "events" / "client_activity" / "events.jsonl")


def _connected(client_id: str, active_desktop: str) -> ConnectionRegistration:
    """A desktop window's live registration as the broadcaster reports it (the desktop "" before it reported one)."""
    return ConnectionRegistration(client_id=client_id, active_desktop=active_desktop, is_pop_out=False)


def _pop_out(client_id: str) -> ConnectionRegistration:
    return ConnectionRegistration(client_id=client_id, active_desktop="", is_pop_out=True)


def test_messages_are_appended_truncated_and_read_back_in_order(tmp_path: Path) -> None:
    log = _log(tmp_path)
    assert log.read_events() == []
    log.append_message(message_report("c1", "home", "chat", "agent-1", "x" * (MESSAGE_TEXT_TRUNCATION_LIMIT + 5)))
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
        log.append_message(message_report("c1", "home", "chat", "agent-1", f"m{index}"))
    log.append_desktop_switch("c1", "home", "research")
    log.append_message(message_report("c2", "home", "chat", "agent-2", "hello"))
    summaries = summarize_client_activity(log.read_events(), [_connected("c2", "notes")])
    assert [summary.client_id for summary in summaries] == ["c2", "c1"]
    first, second = summaries[1], summaries[0]
    assert first.active_desktop == "research"
    assert first.is_connected is False
    assert [message.text for message in first.recent_messages] == [
        f"m{index}" for index in range(2, RECENT_MESSAGES_PER_CLIENT + 2)
    ]
    assert (first.recent_messages[0].app, first.recent_messages[0].key) == ("chat", "agent-1")
    # The live registration outranks the log for the desktop a connected client is on.
    assert second.is_connected is True and second.active_desktop == "notes"


def test_a_window_open_advances_last_seen_and_nothing_else_and_an_unplaced_open_lists_no_client(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)
    log.append_message(message_report("c1", "home", "chat", "agent-1", "hello"))
    log.append_window_opened("c1", "research", "files", "w1", is_new=True, requester="")
    log.append_window_opened("", "research", "files", "w2", is_new=True, requester="chat")
    events = log.read_events()

    (summary,) = summarize_client_activity(events, [])

    assert summary.client_id == "c1"
    assert summary.last_seen == events[1]["timestamp"] and summary.last_seen > events[0]["timestamp"]
    assert summary.active_desktop == "home"
    assert [message.text for message in summary.recent_messages] == ["hello"]


def test_a_message_settles_the_desktop_and_an_empty_live_desktop_reads_as_none(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_desktop_switch("c1", "home", "research")
    log.append_message(message_report("c1", "notes", "chat", "agent-1", "hello"))
    log.append_desktop_switch("c2", "", "home")
    summaries = summarize_client_activity(log.read_events(), [_connected("c2", "")])
    by_id = {str(summary.client_id): summary for summary in summaries}
    assert by_id["c1"].active_desktop == "notes"
    assert by_id["c2"].active_desktop is None and by_id["c2"].is_connected is True


def test_a_connected_client_with_no_activity_is_still_listed(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message(message_report("c1", "home", "chat", "agent-1", "hello"))
    summaries = summarize_client_activity(log.read_events(), [_connected("c9", "research")])
    assert [summary.client_id for summary in summaries] == ["c1", "c9"]
    silent = summaries[1]
    assert silent.is_connected is True
    assert silent.active_desktop == "research"
    assert silent.recent_messages == () and silent.last_seen == ""


def test_a_pop_out_connects_its_client_without_supplying_a_desktop(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_desktop_switch("c1", "home", "research")
    summaries = summarize_client_activity(
        log.read_events(), [_connected("c1", "notes"), _pop_out("c1"), _pop_out("c2")]
    )
    by_id = {str(summary.client_id): summary for summary in summaries}
    # Its main window's registration settles c1's desktop, whichever order the registrations come in.
    assert by_id["c1"].active_desktop == "notes"
    # A client with only a pop-out open is connected, on no desktop the pop-out could name.
    assert by_id["c2"].is_connected is True and by_id["c2"].active_desktop is None


def test_a_message_to_a_page_without_a_marker_is_summarized_with_an_empty_key(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message(message_report("c1", "home", "files", "", "open the notes"))
    (summary,) = summarize_client_activity(log.read_events(), [])
    assert (summary.recent_messages[0].app, summary.recent_messages[0].key) == ("files", "")


def test_a_client_logged_off_its_rules_is_left_out_of_the_summary(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message(message_report("c1", "home", "chat", "agent-1", "hello"))
    # Lines a writer outside the report model could have left: a client id and a desktop id off their rules.
    off_rule_events = [
        {"type": DESKTOP_SWITCH_EVENT_TYPE, "client_id": "not a client id", "to_desktop_id": "home"},
        {"type": DESKTOP_SWITCH_EVENT_TYPE, "client_id": "c2", "to_desktop_id": "Not A Desktop"},
    ]

    summaries = summarize_client_activity([*log.read_events(), *off_rule_events], [])

    assert [summary.client_id for summary in summaries] == ["c1"]


def test_the_last_client_to_message_a_page_is_found(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message(message_report("c1", "home", "chat", "agent-1", "one"))
    log.append_message(message_report("c2", "home", "chat", "agent-1", "two"))
    log.append_message(message_report("c3", "home", "chat", "agent-2", "three"))
    events = log.read_events()
    assert find_client_id_for_page(events, "chat", "agent-1") == "c2"
    assert find_client_id_for_page(events, "chat", "agent-9") is None
    assert find_client_id_for_page(events, "chat", "") is None


def test_unparsable_lines_are_skipped(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message(message_report("c1", "home", "chat", "agent-1", "one"))
    with log.events_path.open("a") as event_file:
        event_file.write("not json\n")
    assert len(log.read_events()) == 1


_T0 = datetime(2026, 11, 5, 17, 22, 10, tzinfo=timezone.utc)


def test_a_window_input_is_written_once_per_client_window_and_minute_whatever_its_kind(tmp_path: Path) -> None:
    """Section 6.2 of the app-usage-signals spec: one line per client, window and minute; the first report of
    the minute is what the line says, a later one in the same minute writes nothing, and another client's or
    another window's report in that minute is its own line."""
    log = _log(tmp_path)
    assert log.append_window_input("c1", "home", "worldbuilder", "w1", WindowInputKind.INPUT, _T0) is True
    assert log.append_window_input("c1", "home", "worldbuilder", "w1", WindowInputKind.INPUT, _T0) is False
    assert (
        log.append_window_input("c1", "home", "worldbuilder", "w1", WindowInputKind.FOCUS, _T0 + timedelta(seconds=40))
        is False
    )
    assert log.append_window_input("c2", "home", "worldbuilder", "w1", WindowInputKind.FOCUS, _T0) is True
    assert log.append_window_input("c1", "home", "notes", "w2", WindowInputKind.INPUT, _T0) is True
    assert (
        log.append_window_input("c1", "home", "worldbuilder", "w1", WindowInputKind.INPUT, _T0 + timedelta(minutes=1))
        is True
    )

    events = log.read_events()
    assert [(event["client_id"], event["window_id"], event["kind"], event["timestamp"]) for event in events] == [
        ("c1", "w1", "input", "2026-11-05T17:22:10.000000000Z"),
        ("c2", "w1", "focus", "2026-11-05T17:22:10.000000000Z"),
        ("c1", "w2", "input", "2026-11-05T17:22:10.000000000Z"),
        ("c1", "w1", "input", "2026-11-05T17:23:10.000000000Z"),
    ]
    assert events[0]["type"] == "window_input" and events[0]["source"] == "client_activity" and events[0]["event_id"]
    assert events[0]["app"] == "worldbuilder" and events[0]["desktop_id"] == "home"
    assert all("path" not in event and "title" not in event for event in events)


def test_a_windows_minute_is_forgotten_once_it_is_old_enough_to_be_written_again(tmp_path: Path) -> None:
    """The once-per-minute memory is pruned of minutes older than its window on every write, so a window reported
    again later is written again and the memory holds only the windows in use."""
    log = _log(tmp_path)
    assert log.append_window_input("c1", "home", "notes", "w1", WindowInputKind.INPUT, _T0) is True
    assert (
        log.append_window_input("c1", "home", "notes", "w2", WindowInputKind.INPUT, _T0 + timedelta(minutes=3)) is True
    )
    assert log._last_input_minute_by_client_window.keys() == {("c1", "w2")}
    assert (
        log.append_window_input("c1", "home", "notes", "w1", WindowInputKind.INPUT, _T0 + timedelta(minutes=3)) is True
    )


def test_a_window_input_advances_last_seen_and_nothing_else(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append_message(message_report("c1", "home", "chat", "agent-1", "hello"))
    log.append_window_input("c1", "home", "notes", "w1", WindowInputKind.INPUT, datetime.now(timezone.utc))
    events = log.read_events()
    (summary,) = summarize_client_activity(events, [])
    assert summary.last_seen == events[1]["timestamp"] and summary.last_seen > events[0]["timestamp"]
    assert summary.active_desktop == "home"
    assert [message.text for message in summary.recent_messages] == ["hello"]
    assert find_client_id_for_page(events, "notes", "w1") is None


def test_an_oversized_log_is_archived_aside_and_the_readers_read_the_live_stream_alone(tmp_path: Path) -> None:
    """Section 6.5 of the app-usage-signals spec: past the threshold the stream is staged aside before a write and
    gzipped off-thread, the next line starts a fresh stream, ``read_events`` reads only that stream, and only the
    newest archives are kept."""
    events_path = tmp_path / "events" / "client_activity" / "events.jsonl"
    log = ClientActivityLog(events_path=events_path, archive_threshold_bytes=600)
    # Three lines of about 220 bytes: the stream crosses the threshold on the third, so the next write stages it.
    for index in range(3):
        log.append_desktop_switch("c1", f"desk-{index}", f"desk-{index + 1}")
    assert 600 < events_path.stat().st_size < 1200
    older = events_path.read_bytes()
    # Archives from before, named so they sort before the one made now; only the newest survive the pruning.
    earlier_archives = [events_path.with_name(f"events.jsonl.2020010{day}000000000000.gz") for day in (1, 2, 3)]
    for earlier_archive in earlier_archives:
        earlier_archive.write_bytes(gzip.compress(b"earlier"))

    log.append_window_input("c1", "home", "notes", "w1", WindowInputKind.INPUT, _T0)

    (event,) = log.read_events()
    assert event["type"] == "window_input"
    wait_for(
        lambda: sorted(path.suffix for path in events_path.parent.iterdir())
        == [".gz"] * KEPT_CLIENT_ACTIVITY_ARCHIVE_COUNT + [".jsonl"],
        timeout=10.0,
        error_message="the staged client-activity log was never compressed and the older archives pruned",
    )
    (archive,) = [
        path for path in events_path.parent.iterdir() if path.suffix == ".gz" and path not in earlier_archives
    ]
    assert gzip.decompress(archive.read_bytes()) == older
    assert [path.exists() for path in earlier_archives] == [False, False, True]


def test_one_archive_thread_runs_at_a_time_and_a_later_append_finishes_what_it_missed(tmp_path: Path) -> None:
    """Appends come from request threads: while an archive thread is alive, an append that finds a staged stream
    starts no second one (two compressions of one staged file would write one archive over the other), and the
    first append after it finishes compresses what is still staged."""
    log = _log(tmp_path)
    log.append_desktop_switch("c1", "desk-0", "desk-1")
    staged = log.events_path.with_name("events.jsonl.20200101000000000000.archiving")
    staged.write_bytes(b"older")
    is_released = threading.Event()
    archiving = threading.Thread(target=is_released.wait, daemon=True)
    archiving.start()
    log._archive_thread = archiving

    log.append_desktop_switch("c1", "desk-1", "desk-2")
    assert log._archive_thread is archiving and staged.exists()

    is_released.set()
    archiving.join(timeout=10.0)
    log.append_desktop_switch("c1", "desk-2", "desk-3")
    assert log._archive_thread is not archiving
    wait_for(
        lambda: not staged.exists() and staged.with_suffix(".gz").exists(),
        timeout=10.0,
        error_message="the staged client-activity log was never compressed",
    )
    assert gzip.decompress(staged.with_suffix(".gz").read_bytes()) == b"older"
