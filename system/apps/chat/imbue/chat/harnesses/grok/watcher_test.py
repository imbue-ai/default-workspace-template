"""Tests for the grok transcript loader and session watcher, over grok 1.0.62's own session logs."""

import json
from pathlib import Path
from typing import Any

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.testing import add_grok_session
from imbue.chat.harnesses.grok.testing import append_grok_updates
from imbue.chat.harnesses.grok.testing import find_fixture_scenario
from imbue.chat.harnesses.grok.testing import get_fake_session_dir
from imbue.chat.harnesses.grok.testing import read_fixture_root_ids
from imbue.chat.harnesses.grok.testing import read_fixture_updates
from imbue.chat.harnesses.grok.testing import remove_root_turn
from imbue.chat.harnesses.grok.testing import write_grok_agent_state
from imbue.chat.harnesses.grok.testing import write_root_turn
from imbue.chat.harnesses.grok.watcher import GrokSessionWatcher
from imbue.chat.harnesses.grok.watcher import GrokTranscriptLoader
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import get_model_state_path
from imbue.mngr_grok.api import GrokRootSession
from imbue.mngr_grok.api import GrokRootTurn
from imbue.mngr_grok.api import get_model_state_path as get_grok_model_state_path
from imbue.mngr_grok.api import get_root_turns
from imbue.mngr_grok.api import get_session_dir
from imbue.mngr_grok.api import is_root_turn_in_flight
from imbue.mngr_grok.api import list_root_session_dirs

_BACKGROUND_SUBAGENT_ROOT = "01a156db-07ea-7993-b2e6-dcaa7cb0a81c"
_BACKGROUND_SUBAGENT_CHILD = "01a156db-10ae-70c2-9a0f-a0dff6d5f376"
_SUBAGENT_ENDS_MID_REPLY_ROOT = "5658a8e7-b909-48b5-b230-c5499cc4a3e1"
_SUBAGENT_ENDS_MID_REPLY_CHILD = "01a16809-3142-7c61-82ac-f493c95a71d0"
# Byte offset of the background_subagent root's subagent_spawned record.
_SUBAGENT_SPAWNED_OFFSET = 3737


def _agent_info(agent_state_dir: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-grok-test",
        name="grok-test",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "unused",
        harness=HarnessType.GROK,
    )


_FINISHED = "finished"
_WAKE_PENDING = "wake_pending"


def _build_watcher(
    agent_state_dir: Path,
) -> tuple[GrokSessionWatcher, list[dict[str, Any]], list[tuple[str, tuple[str, ...]]]]:
    """A watcher over ``agent_state_dir``, never started, with what it broadcasts and each report of background
    work's end: (which callback, the task ids)."""
    broadcast: list[dict[str, Any]] = []
    reported: list[tuple[str, tuple[str, ...]]] = []
    watcher = GrokSessionWatcher.build(
        _agent_info(agent_state_dir), lambda _agent_id, events: broadcast.extend(events)
    )
    watcher.set_finished_tasks_callback(lambda task_ids: reported.append((_FINISHED, task_ids)))
    watcher.set_wake_pending_tasks_callback(lambda task_ids: reported.append((_WAKE_PENDING, task_ids)))
    return watcher, broadcast, reported


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


def test_the_fake_agent_layout_is_the_one_mngr_grok_reads(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("background_subagent"))

    assert list_root_session_dirs(tmp_path) == [
        GrokRootSession(
            session_id=_BACKGROUND_SUBAGENT_ROOT, session_dir=get_fake_session_dir(tmp_path, _BACKGROUND_SUBAGENT_ROOT)
        )
    ]
    assert get_session_dir(tmp_path, _BACKGROUND_SUBAGENT_CHILD) == get_fake_session_dir(
        tmp_path, _BACKGROUND_SUBAGENT_CHILD
    )
    assert get_root_turns(tmp_path) == []

    write_root_turn(tmp_path, _BACKGROUND_SUBAGENT_ROOT, "prompt-24618")
    assert get_root_turns(tmp_path) == [
        GrokRootTurn(session_id=_BACKGROUND_SUBAGENT_ROOT, prompt_id="prompt-24618", is_pending_wake=False)
    ]
    write_root_turn(tmp_path, _BACKGROUND_SUBAGENT_ROOT, f"pending-wake:{_BACKGROUND_SUBAGENT_CHILD}")
    assert [turn.is_pending_wake for turn in get_root_turns(tmp_path)] == [True]
    assert not is_root_turn_in_flight(tmp_path, is_pending_wake_counted=False)
    assert is_root_turn_in_flight(tmp_path, is_pending_wake_counted=True)
    remove_root_turn(tmp_path, _BACKGROUND_SUBAGENT_ROOT)
    assert get_root_turns(tmp_path) == []

    assert get_model_state_path(HarnessType.GROK, tmp_path) == get_grok_model_state_path(tmp_path)
    watcher, _broadcast, _finished = _build_watcher(tmp_path)
    (watched_dir,) = watcher._watch_paths()
    assert watched_dir in get_fake_session_dir(tmp_path, _BACKGROUND_SUBAGENT_CHILD).parents


def test_reads_serve_the_transcript_without_the_thread_having_run(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("tool"))
    watcher, _broadcast, _finished = _build_watcher(tmp_path)

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
    assert {event["agent_id"] for event in watcher.get_all_events()} == {"agent-grok-test"}


def test_top_level_sessions_interleave_by_time_in_one_stream(tmp_path: Path) -> None:
    """``/new`` opens a second top-level session and ``--resume`` returns to the first, so neither session's log
    alone is in order; the chat reads them as one conversation in the order the turns happened."""
    scenario_dir = find_fixture_scenario("new_session_and_resume")
    write_grok_agent_state(tmp_path, scenario_dir)
    first_root, second_root = read_fixture_root_ids(scenario_dir)
    watcher, _broadcast, _finished = _build_watcher(tmp_path)

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


def test_a_subagents_child_session_is_a_lane_of_its_own_kept_out_of_the_main_stream(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("background_subagent"))
    watcher, _broadcast, _finished = _build_watcher(tmp_path)

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
    watcher, _broadcast, _finished = _build_watcher(tmp_path)
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
    watcher, broadcast, _finished = _build_watcher(tmp_path)

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


def test_the_end_of_background_work_whose_wake_turn_already_started_is_reported_finished_once(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("background_subagent"))
    watcher, broadcast, reported = _build_watcher(tmp_path)

    watcher._emit_cycle()
    assert broadcast
    assert reported == [(_FINISHED, (_BACKGROUND_SUBAGENT_CHILD,))]
    watcher._emit_cycle()
    assert reported == [(_FINISHED, (_BACKGROUND_SUBAGENT_CHILD,))]


def test_a_background_shells_end_is_reported_by_its_task_id(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("background_shell"))
    watcher, _broadcast, reported = _build_watcher(tmp_path)

    watcher._emit_cycle()
    assert reported == [(_FINISHED, ("01a156da-5fa8-7e01-bed9-b3e2112a053a",))]


def test_work_whose_wake_is_still_to_come_is_reported_before_the_cycles_events_and_work_awaiting_none_after(
    tmp_path: Path,
) -> None:
    """A shell the model read to its end and one it killed start no turn; a monitor that ends on its own starts one,
    and a turn ending in the same cycle must count it as still to come. Once that turn starts, it awaits nothing."""
    scenario_dir = find_fixture_scenario("tools")
    (session_id,) = read_fixture_root_ids(scenario_dir)
    updates = read_fixture_updates(scenario_dir, session_id)
    monitor_wake_offset = 55447
    add_grok_session(tmp_path, session_id, updates[:monitor_wake_offset], is_root=True)
    watcher, broadcast, _reported = _build_watcher(tmp_path)
    reported_with_broadcast_count: list[tuple[str, tuple[str, ...], int]] = []
    watcher.set_finished_tasks_callback(
        lambda task_ids: reported_with_broadcast_count.append((_FINISHED, task_ids, len(broadcast)))
    )
    watcher.set_wake_pending_tasks_callback(
        lambda task_ids: reported_with_broadcast_count.append((_WAKE_PENDING, task_ids, len(broadcast)))
    )

    watcher._emit_cycle()
    first_cycle_count = len(broadcast)
    append_grok_updates(tmp_path, session_id, updates[monitor_wake_offset:])
    watcher._emit_cycle()

    assert reported_with_broadcast_count == [
        (_WAKE_PENDING, ("01a167b8-138d-7a02-9fbe-d755f98795ab",), 0),
        (
            _FINISHED,
            ("01a167b8-0b72-7232-8e5a-adc5b8d864cf", "01a167b8-13fa-7552-8f62-fad61a50e14e"),
            first_cycle_count,
        ),
        (_FINISHED, ("01a167b8-138d-7a02-9fbe-d755f98795ab",), len(broadcast)),
    ]
    assert len(broadcast) > first_cycle_count > 0


def test_a_subagent_ending_during_a_later_turn_is_reported_before_that_turns_end_and_its_split_reply_grows_in_place(
    tmp_path: Path,
) -> None:
    """The chat waits on a subagent until the wake turn it starts, the turn it ended in included."""
    scenario_dir = find_fixture_scenario("subagent_ends_mid_reply")
    root_id, child_id = _SUBAGENT_ENDS_MID_REPLY_ROOT, _SUBAGENT_ENDS_MID_REPLY_CHILD
    root_lines = read_fixture_updates(scenario_dir, root_id).splitlines(keepends=True)
    add_grok_session(tmp_path, root_id, b"", is_root=True)
    add_grok_session(tmp_path, child_id, read_fixture_updates(scenario_dir, child_id), is_root=False)
    watcher, broadcast, reported = _build_watcher(tmp_path)

    reported_at: list[tuple[str, str]] = []
    reply_texts: list[str] = []
    reply_id = f"grok-{root_id}-9564-text"
    for line in root_lines:
        already_broadcast, already_reported = len(broadcast), len(reported)
        append_grok_updates(tmp_path, root_id, line)
        watcher._emit_cycle()
        reply_texts.extend(event["text"] for event in broadcast[already_broadcast:] if event["event_id"] == reply_id)
        reported_at.extend(
            (json.loads(line)["params"]["update"]["sessionUpdate"], callback)
            for callback, _task_ids in reported[already_reported:]
        )

    assert reported == [(_WAKE_PENDING, (child_id,)), (_FINISHED, (child_id,))]
    assert reported_at == [("subagent_finished", _WAKE_PENDING), ("user_message_chunk", _FINISHED)]
    assert [text.split()[-1] for text in reply_texts] == ["w25", "w27"]
    assert len([event for event in watcher.get_all_events() if event.get("text", "").startswith("STUB_SLOW")]) == 1


def test_a_partial_trailing_record_waits_for_the_rest_of_its_line(tmp_path: Path) -> None:
    updates = read_fixture_updates(find_fixture_scenario("plain"), "01a156d6-f927-7091-91a4-f6692a5800da")
    lines = updates.splitlines(keepends=True)
    reply_index = next(index for index, line in enumerate(lines) if b'"agent_message_chunk"' in line)
    before_reply = b"".join(lines[:reply_index])
    reply = lines[reply_index]
    add_grok_session(tmp_path, "01a156d6-f927-7091-91a4-f6692a5800da", before_reply + reply[:40], is_root=True)
    watcher, _broadcast, _finished = _build_watcher(tmp_path)
    assert [_summary(event)[0] for event in watcher.get_all_events()] == ["special", "user"]

    append_grok_updates(tmp_path, "01a156d6-f927-7091-91a4-f6692a5800da", reply[40:])
    assert [_summary(event)[0] for event in watcher.get_all_events()] == ["special", "user", "text"]


def test_a_malformed_line_is_skipped_and_the_rest_still_reads(tmp_path: Path) -> None:
    updates = read_fixture_updates(find_fixture_scenario("tool"), "01a156d9-1df2-7933-9165-d4ee061dc4d5")
    add_grok_session(tmp_path, "01a156d9-1df2-7933-9165-d4ee061dc4d5", b'{"torn": \n' + updates, is_root=True)
    watcher, _broadcast, _finished = _build_watcher(tmp_path)
    assert len(watcher.get_all_events()) == 6


def test_a_log_that_shrinks_is_read_again_without_duplicating_its_events(tmp_path: Path) -> None:
    session_id = "01a156d9-1df2-7933-9165-d4ee061dc4d5"
    updates = read_fixture_updates(find_fixture_scenario("tool"), session_id)
    updates_path = add_grok_session(tmp_path, session_id, updates, is_root=True)
    watcher, _broadcast, _finished = _build_watcher(tmp_path)
    before = [event["event_id"] for event in watcher.get_all_events()]

    updates_path.write_bytes(updates[: len(updates) // 2])
    assert [event["event_id"] for event in watcher.get_all_events()] == before


def test_a_log_that_shrinks_mid_turn_is_parsed_again_from_a_fresh_start(tmp_path: Path) -> None:
    """A re-read of a turn's first lines must not join them to what the first read made of them."""
    session_id = "01a156d9-1df2-7933-9165-d4ee061dc4d5"
    lines = read_fixture_updates(find_fixture_scenario("tool"), session_id).splitlines(keepends=True)
    user_index = next(index for index, line in enumerate(lines) if b'"user_message_chunk"' in line)
    updates_path = add_grok_session(tmp_path, session_id, b"".join(lines[: user_index + 1]), is_root=True)
    watcher, _broadcast, _finished = _build_watcher(tmp_path)
    assert [event.get("content") for event in watcher.get_all_events() if event["type"] == "user_message"] == [
        "TOOLTEST please"
    ]

    updates_path.write_bytes(b"".join(lines[:user_index]))
    watcher.get_all_events()
    append_grok_updates(tmp_path, session_id, b"".join(lines[user_index:]))

    events = watcher.get_all_events()
    assert [event["content"] for event in events if event["type"] == "user_message"] == ["TOOLTEST please"]
    assert [_summary(event)[0] for event in events] == ["special", "user", "call", "result", "text", "special"]


def test_payloads_are_served_from_their_records_and_thinking_from_its_thought_line(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("streaming"))
    watcher, _broadcast, _finished = _build_watcher(tmp_path)
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
    watcher, _broadcast, _finished = _build_watcher(tmp_path)

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
    loader = GrokTranscriptLoader.build_loader(_agent_info(tmp_path))

    assert [_summary(event) for event in loader.get_all_events()][:3] == [
        ("special", "turn_started"),
        ("user", "SUBAGENTBG spawn one in the background"),
        ("call", "spawn_subagent"),
    ]
    assert len(loader.get_all_events(session_id=_BACKGROUND_SUBAGENT_CHILD)) == 3
    assert loader.get_subagent_metadata(_BACKGROUND_SUBAGENT_CHILD) is not None


def test_an_agent_with_no_session_yet_reads_empty(tmp_path: Path) -> None:
    watcher, broadcast, finished = _build_watcher(tmp_path)

    assert watcher.get_all_events() == []
    assert watcher.get_total_event_count() == 0
    assert watcher.get_event_offset("grok-anything") == -1
    watcher._emit_cycle()
    assert (broadcast, finished) == ([], [])
