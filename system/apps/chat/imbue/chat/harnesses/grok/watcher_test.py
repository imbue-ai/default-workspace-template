"""Tests for the grok transcript loader and session watcher, over grok 1.0.62's own session logs."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.activity_state import ACTIVE_MARKER_FILENAME
from imbue.chat.harnesses.grok.queue_tracker import MAX_DELIVERY_ATTEMPTS
from imbue.chat.harnesses.grok.testing import add_grok_session
from imbue.chat.harnesses.grok.testing import append_grok_updates
from imbue.chat.harnesses.grok.testing import find_fixture_scenario
from imbue.chat.harnesses.grok.testing import get_fake_session_dir
from imbue.chat.harnesses.grok.testing import make_grok_agent_info
from imbue.chat.harnesses.grok.testing import make_grok_agent_state_dir
from imbue.chat.harnesses.grok.testing import read_fixture_root_ids
from imbue.chat.harnesses.grok.testing import read_fixture_updates
from imbue.chat.harnesses.grok.testing import record_root_session
from imbue.chat.harnesses.grok.testing import user_prompt_line
from imbue.chat.harnesses.grok.testing import write_grok_agent_state
from imbue.chat.harnesses.grok.watcher import GrokSessionWatcher
from imbue.chat.harnesses.grok.watcher import GrokTranscriptLoader
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_grok.grok_config import PROCESS_STARTED_MARKER_FILENAME
from imbue.mngr_grok.grok_sessions import find_local_session_dir
from imbue.mngr_grok.grok_sessions import list_local_root_session_dirs

_AGENT_ID = "agent-grok-test"
_PLAIN_ROOT = "01a156d6-f927-7091-91a4-f6692a5800da"
_TOOL_ROOT = "01a156d9-1df2-7933-9165-d4ee061dc4d5"
_BACKGROUND_SUBAGENT_ROOT = "01a156db-07ea-7993-b2e6-dcaa7cb0a81c"
_BACKGROUND_SUBAGENT_CHILD = "01a156db-10ae-70c2-9a0f-a0dff6d5f376"
_SUBAGENT_ENDS_MID_REPLY_ROOT = "5658a8e7-b909-48b5-b230-c5499cc4a3e1"
_SUBAGENT_ENDS_MID_REPLY_CHILD = "01a16809-3142-7c61-82ac-f493c95a71d0"
# Byte offset of the background_subagent root's subagent_spawned record.
_SUBAGENT_SPAWNED_OFFSET = 3737
_LIVE_TAIL_TIMEOUT_SECONDS = 10.0
_SHORT_WITNESS_SECONDS = 0.3
# A record time after every captured session's, for the prompts the scripted grok logs.
_PROMPT_CLOCK_MS = 1_800_000_000_000


def _build_watcher(agent_state_dir: Path) -> tuple[GrokSessionWatcher, list[dict[str, Any]]]:
    """A watcher over ``agent_state_dir``, never started, with what it broadcasts."""
    broadcast: list[dict[str, Any]] = []
    watcher = GrokSessionWatcher.build(
        make_grok_agent_info(agent_state_dir, _AGENT_ID, "grok-test"),
        lambda _agent_id, events: broadcast.extend(events),
    )
    return watcher, broadcast


def _summary(event: dict[str, Any]) -> tuple[Any, ...]:
    match event["type"]:
        case "special":
            return ("special", event["kind"])
        case "user_message":
            return ("user", event["content"][:40])
        case "assistant_message":
            if event["tool_calls"]:
                return ("call", event["tool_calls"][0]["tool_name"])
            return ("text", event["text"][:40])
        case "tool_result":
            return ("result", event["tool_name"], event.get("error_snippet"))
        case _:
            return (event["type"],)


def test_the_fake_agent_layout_is_the_one_mngr_reads(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("background_subagent"))

    assert list_local_root_session_dirs(tmp_path) == [get_fake_session_dir(tmp_path, _BACKGROUND_SUBAGENT_ROOT)]
    assert find_local_session_dir(tmp_path, _BACKGROUND_SUBAGENT_CHILD) == get_fake_session_dir(
        tmp_path, _BACKGROUND_SUBAGENT_CHILD
    )
    watcher, _broadcast = _build_watcher(tmp_path)
    (watched_dir,) = watcher._watch_paths()
    assert watched_dir in get_fake_session_dir(tmp_path, _BACKGROUND_SUBAGENT_CHILD).parents


def test_reads_serve_the_transcript_without_the_thread_having_run(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("tool"))
    watcher, _broadcast = _build_watcher(tmp_path)

    assert [_summary(event) for event in watcher.get_all_events()] == [
        ("special", "turn_started"),
        ("user", "TOOLTEST please"),
        ("call", "run_terminal_command"),
        ("result", "run_terminal_command", None),
        ("text", "STUB_REPLY: tool finished"),
        ("special", "turn_completed"),
    ]
    assert watcher.get_total_event_count() == 6
    assert [_summary(event) for event in watcher.get_tail_events(2)] == [
        ("text", "STUB_REPLY: tool finished"),
        ("special", "turn_completed"),
    ]
    assert {event["agent_id"] for event in watcher.get_all_events()} == {_AGENT_ID}


def test_top_level_sessions_interleave_by_time_in_one_stream(tmp_path: Path) -> None:
    """``/new`` opens a second top-level session and ``--resume`` returns to the first, so neither session's log
    alone is in order; the chat reads them as one conversation in the order the turns happened."""
    scenario_dir = find_fixture_scenario("new_session_and_resume")
    write_grok_agent_state(tmp_path, scenario_dir)
    first_root, second_root = read_fixture_root_ids(scenario_dir)
    watcher, _broadcast = _build_watcher(tmp_path)

    user_turns = [
        (event["session_id"], event["content"])
        for event in watcher.get_all_events()
        if event["type"] == "user_message"
    ]
    assert user_turns == [
        (first_root, "first prompt in N2"),
        (second_root, "prompt in the /new session of N2"),
        (first_root, "second prompt in N2 after resume"),
    ]
    timestamps = [event["timestamp"] for event in watcher.get_all_events()]
    assert timestamps == sorted(timestamps)
    assert all(watcher.is_main_session_event(event) for event in watcher.get_all_events())


def test_a_top_level_session_whose_directory_appears_late_is_read_once_it_does(tmp_path: Path) -> None:
    """mngr records a top-level session at its start, which can come before grok makes its directory."""
    record_root_session(tmp_path, _PLAIN_ROOT)
    watcher, _broadcast = _build_watcher(tmp_path)
    assert watcher.get_all_events() == []

    updates = read_fixture_updates(find_fixture_scenario("plain"), _PLAIN_ROOT)
    get_fake_session_dir(tmp_path, _PLAIN_ROOT).mkdir(parents=True)
    append_grok_updates(tmp_path, _PLAIN_ROOT, updates)

    assert [_summary(event)[0] for event in watcher.get_all_events()] == ["special", "user", "text", "special"]


def test_a_subagents_child_session_is_a_lane_of_its_own_kept_out_of_the_main_stream(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("background_subagent"))
    watcher, _broadcast = _build_watcher(tmp_path)

    main_events = watcher.get_all_events()
    child_events = watcher.get_all_events(session_id=_BACKGROUND_SUBAGENT_CHILD)
    assert {event["session_id"] for event in main_events} == {_BACKGROUND_SUBAGENT_ROOT}
    assert [_summary(event) for event in child_events] == [
        ("user", "say hi from the subagent"),
        ("text", "STUB_CHILD: slow child one two three fou"),
        ("special", "turn_completed"),
    ]
    metadata = watcher.get_subagent_metadata(_BACKGROUND_SUBAGENT_CHILD)
    assert metadata == {
        "agent_type": "general-purpose",
        "description": "stub subagent bg",
        "session_id": _BACKGROUND_SUBAGENT_CHILD,
    }
    (spawn_call,) = [event["tool_calls"][0] for event in main_events if event.get("tool_calls")]
    assert spawn_call["tool_name"] == "spawn_subagent"
    assert spawn_call["subagent_metadata"] == metadata
    assert all(watcher.is_main_session_event(event) for event in main_events)
    assert not any(watcher.is_main_session_event(event) for event in child_events)
    assert watcher.is_main_session_event({"type": "agent_switch"})
    assert watcher.get_all_events(session_id=_BACKGROUND_SUBAGENT_ROOT) == []
    assert watcher.get_subagent_metadata(_BACKGROUND_SUBAGENT_ROOT) is None


def test_a_child_session_whose_directory_appears_late_is_tailed_once_it_does(tmp_path: Path) -> None:
    scenario_dir = find_fixture_scenario("background_subagent")
    add_grok_session(
        tmp_path,
        _BACKGROUND_SUBAGENT_ROOT,
        read_fixture_updates(scenario_dir, _BACKGROUND_SUBAGENT_ROOT),
        is_root=True,
    )
    watcher, _broadcast = _build_watcher(tmp_path)
    assert watcher.get_all_events(session_id=_BACKGROUND_SUBAGENT_CHILD) == []
    assert watcher.get_subagent_metadata(_BACKGROUND_SUBAGENT_CHILD) is not None

    add_grok_session(
        tmp_path,
        _BACKGROUND_SUBAGENT_CHILD,
        read_fixture_updates(scenario_dir, _BACKGROUND_SUBAGENT_CHILD),
        is_root=False,
    )
    assert len(watcher.get_all_events(session_id=_BACKGROUND_SUBAGENT_CHILD)) == 3


def test_the_live_tail_broadcasts_each_event_once_and_upgrades_the_spawn_card_in_place(tmp_path: Path) -> None:
    scenario_dir = find_fixture_scenario("background_subagent")
    root_updates = read_fixture_updates(scenario_dir, _BACKGROUND_SUBAGENT_ROOT)
    add_grok_session(tmp_path, _BACKGROUND_SUBAGENT_ROOT, root_updates[:_SUBAGENT_SPAWNED_OFFSET], is_root=True)
    add_grok_session(
        tmp_path,
        _BACKGROUND_SUBAGENT_CHILD,
        read_fixture_updates(scenario_dir, _BACKGROUND_SUBAGENT_CHILD),
        is_root=False,
    )
    watcher, broadcast = _build_watcher(tmp_path)

    watcher._emit_cycle()
    (spawn_event,) = [event for event in broadcast if event.get("tool_calls")]
    assert "subagent_metadata" not in spawn_event["tool_calls"][0]
    first_cycle_count = len(broadcast)

    append_grok_updates(tmp_path, _BACKGROUND_SUBAGENT_ROOT, root_updates[_SUBAGENT_SPAWNED_OFFSET:])
    watcher._emit_cycle()
    second_cycle = broadcast[first_cycle_count:]
    assert second_cycle[0]["event_id"] == spawn_event["event_id"]
    assert second_cycle[0]["tool_calls"][0]["subagent_metadata"]["session_id"] == _BACKGROUND_SUBAGENT_CHILD
    first_ids = [event["event_id"] for event in broadcast[:first_cycle_count]]
    new_ids = [event["event_id"] for event in second_cycle[1:]]
    assert len(set(first_ids)) == len(first_ids)
    assert len(set(new_ids)) == len(new_ids)
    assert not set(first_ids) & set(new_ids)
    expected_ids = {
        event["event_id"]
        for session_id in (None, _BACKGROUND_SUBAGENT_CHILD)
        for event in watcher.get_all_events(session_id=session_id)
    }
    assert set(first_ids) | set(new_ids) == expected_ids

    watcher._emit_cycle()
    assert len(broadcast) == first_cycle_count + len(second_cycle)


def test_a_reply_split_by_a_subagent_ending_mid_stream_grows_in_place_on_the_live_tail(tmp_path: Path) -> None:
    scenario_dir = find_fixture_scenario("subagent_ends_mid_reply")
    root_lines = read_fixture_updates(scenario_dir, _SUBAGENT_ENDS_MID_REPLY_ROOT).splitlines(keepends=True)
    add_grok_session(tmp_path, _SUBAGENT_ENDS_MID_REPLY_ROOT, b"", is_root=True)
    add_grok_session(
        tmp_path,
        _SUBAGENT_ENDS_MID_REPLY_CHILD,
        read_fixture_updates(scenario_dir, _SUBAGENT_ENDS_MID_REPLY_CHILD),
        is_root=False,
    )
    watcher, broadcast = _build_watcher(tmp_path)

    reply_texts: list[str] = []
    reply_id = f"grok-{_SUBAGENT_ENDS_MID_REPLY_ROOT}-9564-text"
    for line in root_lines:
        already_broadcast = len(broadcast)
        append_grok_updates(tmp_path, _SUBAGENT_ENDS_MID_REPLY_ROOT, line)
        watcher._emit_cycle()
        reply_texts.extend(event["text"] for event in broadcast[already_broadcast:] if event["event_id"] == reply_id)

    assert [text.split()[-1] for text in reply_texts] == ["w25", "w27"]
    assert len([event for event in watcher.get_all_events() if event.get("text", "").startswith("STUB_SLOW")]) == 1


def test_a_started_watcher_broadcasts_only_what_is_written_after_it_starts(tmp_path: Path) -> None:
    scenario_dir = find_fixture_scenario("tool")
    lines = read_fixture_updates(scenario_dir, _TOOL_ROOT).splitlines(keepends=True)
    user_index = next(index for index, line in enumerate(lines) if b'"user_message_chunk"' in line)
    add_grok_session(tmp_path, _TOOL_ROOT, b"".join(lines[: user_index + 1]), is_root=True)
    watcher, broadcast = _build_watcher(tmp_path)

    watcher.start()
    try:
        append_grok_updates(tmp_path, _TOOL_ROOT, b"".join(lines[user_index + 1 :]))
        wait_for(
            lambda: any(event["type"] == "special" and event["kind"] == "turn_completed" for event in broadcast),
            timeout=_LIVE_TAIL_TIMEOUT_SECONDS,
            poll_interval=0.05,
            error_message="the started watcher never broadcast the turn's end",
        )
    finally:
        watcher.stop()

    assert [_summary(event)[0] for event in broadcast] == ["call", "result", "text", "special"]


def test_a_partial_trailing_record_waits_for_the_rest_of_its_line(tmp_path: Path) -> None:
    updates = read_fixture_updates(find_fixture_scenario("plain"), _PLAIN_ROOT)
    lines = updates.splitlines(keepends=True)
    reply_index = next(index for index, line in enumerate(lines) if b'"agent_message_chunk"' in line)
    before_reply = b"".join(lines[:reply_index])
    reply = lines[reply_index]
    add_grok_session(tmp_path, _PLAIN_ROOT, before_reply + reply[:40], is_root=True)
    watcher, _broadcast = _build_watcher(tmp_path)
    assert [_summary(event)[0] for event in watcher.get_all_events()] == ["special", "user"]

    append_grok_updates(tmp_path, _PLAIN_ROOT, reply[40:])
    assert [_summary(event)[0] for event in watcher.get_all_events()] == ["special", "user", "text"]


def test_a_malformed_line_is_skipped_and_the_rest_still_reads(tmp_path: Path) -> None:
    updates = read_fixture_updates(find_fixture_scenario("tool"), _TOOL_ROOT)
    add_grok_session(tmp_path, _TOOL_ROOT, b'{"torn": \n' + updates, is_root=True)
    watcher, _broadcast = _build_watcher(tmp_path)
    assert len(watcher.get_all_events()) == 6


def test_a_log_that_shrinks_is_read_again_without_duplicating_its_events(tmp_path: Path) -> None:
    updates = read_fixture_updates(find_fixture_scenario("tool"), _TOOL_ROOT)
    updates_path = add_grok_session(tmp_path, _TOOL_ROOT, updates, is_root=True)
    watcher, _broadcast = _build_watcher(tmp_path)
    before = [event["event_id"] for event in watcher.get_all_events()]

    updates_path.write_bytes(updates[: len(updates) // 2])
    assert [event["event_id"] for event in watcher.get_all_events()] == before


def test_a_log_that_shrinks_mid_turn_is_parsed_again_from_a_fresh_start(tmp_path: Path) -> None:
    """A re-read of a turn's first lines must not join them to what the first read made of them."""
    lines = read_fixture_updates(find_fixture_scenario("tool"), _TOOL_ROOT).splitlines(keepends=True)
    user_index = next(index for index, line in enumerate(lines) if b'"user_message_chunk"' in line)
    updates_path = add_grok_session(tmp_path, _TOOL_ROOT, b"".join(lines[: user_index + 1]), is_root=True)
    watcher, _broadcast = _build_watcher(tmp_path)
    assert [event.get("content") for event in watcher.get_all_events() if event["type"] == "user_message"] == [
        "TOOLTEST please"
    ]

    updates_path.write_bytes(b"".join(lines[:user_index]))
    watcher.get_all_events()
    append_grok_updates(tmp_path, _TOOL_ROOT, b"".join(lines[user_index:]))

    events = watcher.get_all_events()
    assert [event["content"] for event in events if event["type"] == "user_message"] == ["TOOLTEST please"]
    assert [_summary(event)[0] for event in events] == ["special", "user", "call", "result", "text", "special"]


def test_payloads_are_served_from_their_records_and_thinking_from_its_thought_line(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("streaming"))
    watcher, _broadcast = _build_watcher(tmp_path)
    events = watcher.get_all_events()

    (call_event,) = [event for event in events if event.get("tool_calls")]
    call_id = call_event["tool_calls"][0]["tool_call_id"]
    call_detail = watcher.get_event_detail(call_event["event_id"])
    assert call_detail is not None
    assert json.loads(call_detail["inputs_by_tool_call_id"][call_id])["command"].startswith("echo first")

    (result_event,) = [event for event in events if event["type"] == "tool_result"]
    result_detail = watcher.get_event_detail(result_event["event_id"])
    assert result_detail is not None
    assert result_detail["output"] == "exit: 0\nfirst\n"
    assert len(result_detail["output"]) == result_event["output_chars"]

    (thinking_event,) = [event for event in events if event.get("has_thinking")]
    thinking_detail = watcher.get_event_detail(thinking_event["event_id"])
    assert thinking_detail is not None
    assert thinking_detail["thinking"].startswith("thinking about it")
    assert thinking_detail["output"] is None


def test_an_interrupted_tool_shows_its_snippet_and_has_no_payload_to_serve(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("interrupt"))
    watcher, _broadcast = _build_watcher(tmp_path)

    (interrupted,) = [event for event in watcher.get_all_events() if event["type"] == "tool_result"]
    assert (interrupted["is_error"], interrupted["error_snippet"], interrupted["output_chars"]) == (
        True,
        "Interrupted.",
        0,
    )
    assert watcher.get_event_detail(interrupted["event_id"]) is None
    assert watcher.get_event_detail("grok-no-such-event") is None


def test_a_loader_reads_an_agents_sessions_with_nothing_watching(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("background_subagent"))
    loader = GrokTranscriptLoader.build_loader(make_grok_agent_info(tmp_path, _AGENT_ID, "grok-test"))

    assert [_summary(event) for event in loader.get_all_events()][:3] == [
        ("special", "turn_started"),
        ("user", "SUBAGENTBG spawn one in the background"),
        ("call", "spawn_subagent"),
    ]
    assert len(loader.get_all_events(session_id=_BACKGROUND_SUBAGENT_CHILD)) == 3
    assert loader.get_subagent_metadata(_BACKGROUND_SUBAGENT_CHILD) is not None


def test_an_agent_with_no_session_yet_reads_empty(tmp_path: Path) -> None:
    watcher, broadcast = _build_watcher(tmp_path)

    assert watcher.get_all_events() == []
    assert watcher.get_total_event_count() == 0
    assert watcher.get_event_offset("grok-anything") == -1
    watcher._emit_cycle()
    assert broadcast == []


class _ScriptedSend:
    """mngr's send of the held block to a scripted grok: whether mngr confirms it, and whether grok logs it."""

    def __init__(self, agent_state_dir: Path, *, is_confirmed: bool, is_logged: bool) -> None:
        self.agent_state_dir = agent_state_dir
        self.is_confirmed = is_confirmed
        self.is_logged = is_logged
        self.draft = ""
        self.sent: list[str] = []
        self.after_send: Callable[[], None] = lambda: None

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        if self.is_logged:
            prompt_line = user_prompt_line(
                _PLAIN_ROOT, self.draft + text, len(self.sent), _PROMPT_CLOCK_MS + len(self.sent)
            )
            append_grok_updates(self.agent_state_dir, _PLAIN_ROOT, prompt_line)
        self.after_send()
        return self.is_confirmed


def _flushing_watcher(
    tmp_path: Path, *, lifecycle_state: str | None = "WAITING", is_confirmed: bool = True, is_logged: bool = True
) -> tuple[GrokSessionWatcher, _ScriptedSend, list[tuple[str, Any]]]:
    """A grok agent with one finished turn and a primed, unstarted watcher whose flush types into a scripted grok.

    The log records each queue snapshot the watcher publishes and each event batch it broadcasts, in order.
    """
    agent_state_dir = make_grok_agent_state_dir(tmp_path)
    add_grok_session(
        agent_state_dir, _PLAIN_ROOT, read_fixture_updates(find_fixture_scenario("plain"), _PLAIN_ROOT), is_root=True
    )
    log: list[tuple[str, Any]] = []
    watcher = GrokSessionWatcher.build(
        make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-flush"),
        lambda _agent_id, events: log.append(("events", [_summary(event) for event in events])),
    )
    watcher._prime()
    watcher.delivery_witness_seconds = _SHORT_WITNESS_SECONDS
    watcher.set_queue_snapshot_callback(
        lambda snapshot: log.append(("queue", [entry["content"] for entry in snapshot]))
    )
    watcher._queue.attach(publish=watcher._publish_snapshot, wake=lambda: None)
    send = _ScriptedSend(agent_state_dir, is_confirmed=is_confirmed, is_logged=is_logged)
    watcher.set_flush_hooks(send, lambda: lifecycle_state, lambda text, sent_after: log.append(("resent", text)))
    return watcher, send, log


def _hold(watcher: GrokSessionWatcher, *texts: str) -> None:
    for text in texts:
        watcher._queue.enqueue(text, "t0", is_turn_open=False)


def test_the_flush_types_the_held_block_once_and_its_chip_leaves_before_its_turn_shows(tmp_path: Path) -> None:
    watcher, send, log = _flushing_watcher(tmp_path)
    _hold(watcher, "first", "second")
    log.clear()

    watcher._attempt_flush()
    watcher._attempt_flush()

    assert send.sent == ["first\nsecond"]
    assert watcher.get_queued_messages() == []
    chip_gone_at = log.index(("queue", []))
    turn_shown_at = next(index for index, entry in enumerate(log) if entry[0] == "events")
    assert chip_gone_at < turn_shown_at
    assert log[turn_shown_at] == ("events", [("special", "turn_started"), ("user", "first\nsecond")])


@pytest.mark.parametrize(
    ("lifecycle_state", "is_marker_present"),
    [
        pytest.param("RUNNING", False, id="mngr_reports_running"),
        pytest.param("WAITING", True, id="the_active_marker_is_ahead_of_the_reported_state"),
        pytest.param("STOPPED", False, id="the_agent_is_stopped"),
        pytest.param(None, False, id="the_agent_is_not_tracked"),
        pytest.param("UNKNOWN", True, id="mngr_cannot_observe_the_agent"),
    ],
)
def test_the_flush_types_nothing_while_a_turn_is_in_flight_or_the_agent_is_not_alive(
    tmp_path: Path, lifecycle_state: str | None, is_marker_present: bool
) -> None:
    watcher, send, _log = _flushing_watcher(tmp_path, lifecycle_state=lifecycle_state)
    if is_marker_present:
        (send.agent_state_dir / ACTIVE_MARKER_FILENAME).touch()
    _hold(watcher, "held")

    watcher._attempt_flush()

    assert send.sent == []
    assert [(entry["content"], entry["is_sending"]) for entry in watcher.get_queued_messages()] == [("held", False)]


def test_the_flush_types_into_a_grok_that_runs_only_background_work(tmp_path: Path) -> None:
    watcher, send, _log = _flushing_watcher(tmp_path, lifecycle_state="RUNNING_BACKGROUND")
    _hold(watcher, "next")

    watcher._attempt_flush()

    assert send.sent == ["next"]
    assert watcher.get_queued_messages() == []


def test_a_block_mngr_confirmed_is_delivered_though_grok_logged_no_turn_for_it(tmp_path: Path) -> None:
    """grok queued it behind a turn that started first, and logs it only once that turn ends: typing it again would
    run it twice."""
    watcher, send, _log = _flushing_watcher(tmp_path, is_logged=False)
    _hold(watcher, "queued inside grok")

    watcher._attempt_flush()

    assert send.sent == ["queued inside grok"]
    assert watcher.get_queued_messages() == []


def test_a_block_mngr_could_not_confirm_is_delivered_once_grok_logs_it_after_a_draft(tmp_path: Path) -> None:
    watcher, send, _log = _flushing_watcher(tmp_path, is_confirmed=False)
    send.draft = "someone typed this at the pane "
    _hold(watcher, "held")

    watcher._attempt_flush()

    assert send.sent == ["held"]
    assert watcher.get_queued_messages() == []


def test_a_block_no_sign_shows_grok_took_is_typed_again_later_and_noted_as_sent_again(tmp_path: Path) -> None:
    watcher, send, log = _flushing_watcher(tmp_path, is_confirmed=False, is_logged=False)
    _hold(watcher, "held")

    watcher._attempt_flush()
    assert [(entry["content"], entry["is_sending"]) for entry in watcher.get_queued_messages()] == [("held", False)]

    send.is_confirmed = True
    send.is_logged = True
    watcher._attempt_flush()

    assert send.sent == ["held", "held"]
    assert watcher.get_queued_messages() == []
    assert ("resent", "held") in log


def test_a_block_that_never_shows_stops_being_typed_after_its_attempts(tmp_path: Path) -> None:
    watcher, send, _log = _flushing_watcher(tmp_path, is_confirmed=False, is_logged=False)
    _hold(watcher, "held")

    for _attempt in range(MAX_DELIVERY_ATTEMPTS + 2):
        watcher._attempt_flush()

    assert send.sent == ["held"] * MAX_DELIVERY_ATTEMPTS
    assert [entry["content"] for entry in watcher.get_queued_messages()] == ["held"]


def test_the_live_emit_holds_back_while_a_flush_waits_to_see_its_block(tmp_path: Path) -> None:
    watcher, send, log = _flushing_watcher(tmp_path)
    emitted_during_the_send: list[list[tuple[str, Any]]] = []

    def emit_during_the_send() -> None:
        before = len(log)
        watcher._emit_cycle()
        emitted_during_the_send.append(log[before:])

    send.after_send = emit_during_the_send
    _hold(watcher, "held")

    watcher._attempt_flush()

    assert emitted_during_the_send == [[]]
    assert [entry for entry in log if entry[0] == "events"] == [
        ("events", [("special", "turn_started"), ("user", "held")])
    ]


def test_the_flush_drops_the_messages_held_for_a_grok_that_relaunched(tmp_path: Path) -> None:
    watcher, send, _log = _flushing_watcher(tmp_path)
    _hold(watcher, "for the old grok")
    (send.agent_state_dir / PROCESS_STARTED_MARKER_FILENAME).touch()

    watcher._attempt_flush()

    assert send.sent == []
    assert watcher.get_queued_messages() == []


def test_a_started_watchers_worker_types_a_held_message_once_mngr_reports_the_turn_over(tmp_path: Path) -> None:
    lifecycle = ["RUNNING"]
    agent_state_dir = make_grok_agent_state_dir(tmp_path)
    add_grok_session(
        agent_state_dir, _PLAIN_ROOT, read_fixture_updates(find_fixture_scenario("plain"), _PLAIN_ROOT), is_root=True
    )
    watcher = GrokSessionWatcher.build(
        make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-worker"), lambda _agent_id, _events: None
    )
    send = _ScriptedSend(agent_state_dir, is_confirmed=True, is_logged=True)
    watcher.set_flush_hooks(send, lambda: lifecycle[0], lambda text, sent_after: None)
    watcher.start()
    try:
        _hold(watcher, "held")
        watcher.notify_idle()
        assert not poll_until(lambda: bool(send.sent), timeout=0.5, poll_interval=0.05)

        lifecycle[0] = "WAITING"
        watcher.notify_idle()
        wait_for(
            lambda: not watcher.get_queued_messages(),
            timeout=_LIVE_TAIL_TIMEOUT_SECONDS,
            poll_interval=0.05,
            error_message="the worker never delivered the held message",
        )
    finally:
        watcher.stop()

    assert send.sent == ["held"]
