"""Tests for the grok transcript loader and session watcher, over grok 1.0.62's own session logs."""

import json
import time
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.harnesses.grok import queue_tracker
from imbue.chat.harnesses.grok.queue_tracker import GrokSendVerdict
from imbue.chat.harnesses.grok.testing import ROOTS_RELATIVE_PATH
from imbue.chat.harnesses.grok.testing import add_grok_session
from imbue.chat.harnesses.grok.testing import append_grok_updates
from imbue.chat.harnesses.grok.testing import find_fixture_scenario
from imbue.chat.harnesses.grok.testing import get_fake_session_dir
from imbue.chat.harnesses.grok.testing import grok_update_line
from imbue.chat.harnesses.grok.testing import make_grok_agent_info
from imbue.chat.harnesses.grok.testing import read_fixture_root_ids
from imbue.chat.harnesses.grok.testing import read_fixture_updates
from imbue.chat.harnesses.grok.testing import remove_root_turn
from imbue.chat.harnesses.grok.testing import write_grok_agent_state
from imbue.chat.harnesses.grok.testing import write_root_turn
from imbue.chat.harnesses.grok.watcher import GrokSessionWatcher
from imbue.chat.harnesses.grok.watcher import GrokTranscriptLoader
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.registry import get_model_state_path
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_grok.api import GrokRootSession
from imbue.mngr_grok.api import GrokRootTurn
from imbue.mngr_grok.api import PROCESS_STARTED_MARKER_FILENAME
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
    return make_grok_agent_info(agent_state_dir, "agent-grok-test", "grok-test")


_FINISHED = "finished"
_WAKE_PENDING = "wake_pending"
# Half the flush worker's retry interval, so only the root-turn watcher's wake sends this soon.
_SOONER_THAN_THE_RETRY_SECONDS = 2.5


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


# The flush worker: the only code that types into grok

_PLAIN_ROOT = "01a156d6-f927-7091-91a4-f6692a5800da"


def _committed_turn_lines(session_id: str, prompt: str, prompt_index: int, timestamp_ms: int) -> bytes:
    """The records grok logs for a submitted prompt it answers: its user chunk, a reply and the turn's end."""
    user_chunk = {
        "sessionUpdate": "user_message_chunk",
        "content": {"type": "text", "text": prompt},
        "_meta": {"modelId": "stub-model", "promptIndex": prompt_index},
    }
    reply = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "STUB_REPLY: ok"}}
    turn_end = {"sessionUpdate": "turn_completed", "prompt_id": f"prompt-{prompt_index}", "stop_reason": "end_turn"}
    return b"".join(
        grok_update_line(session_id, update, timestamp_ms + step)
        for step, update in enumerate((user_chunk, reply, turn_end))
    )


class _ScriptedGrok:
    """A grok behind mngr's send: logs a turn for each sent text, or refuses the send the way mngr does."""

    def __init__(self, agent_state_dir: Path) -> None:
        self.agent_state_dir = agent_state_dir
        self.sent: list[str] = []
        self.refusal: SendFailedError | None = None
        self.is_committing = True
        # mngr types the text but finds no evidence that grok took it.
        self.is_unconfirmed = False
        # Text someone left in grok's composer, which mngr types the next message after.
        self.draft = ""
        self.on_send: Any = None
        self._prompt_index = 1
        self._clock_ms = 1792500000000

    def send(self, text: str) -> bool:
        if self.refusal is not None:
            raise self.refusal
        self.sent.append(text)
        if self.is_committing:
            self.commit(self.draft + text)
            self.draft = ""
        if self.on_send is not None:
            self.on_send()
        if self.is_unconfirmed:
            raise SendFailedError("Message submission was not confirmed", "submission_unconfirmed")
        return True

    def commit(self, prompt: str) -> None:
        """grok logs a user turn holding ``prompt``, and answers it."""
        append_grok_updates(
            self.agent_state_dir,
            _PLAIN_ROOT,
            _committed_turn_lines(_PLAIN_ROOT, prompt, self._prompt_index, self._clock_ms),
        )
        self._prompt_index += 1
        self._clock_ms += 1000


def _flushing_watcher(
    agent_state_dir: Path, *, is_alive: bool = True
) -> tuple[GrokSessionWatcher, _ScriptedGrok, list[dict[str, Any]]]:
    """A primed, unstarted watcher over a grok whose one session already holds a finished turn."""
    write_grok_agent_state(agent_state_dir, find_fixture_scenario("plain"))
    watcher, broadcast, _reported = _build_watcher(agent_state_dir)
    watcher._prime()
    grok = _ScriptedGrok(agent_state_dir)
    watcher.set_flush_hooks(_fail_if_called, lambda: is_alive)
    watcher._queue.bind_session_send(grok.send)
    watcher._queue.attach(publish=watcher._publish_snapshot, wake=lambda: None)
    watcher.delivery_witness_seconds = 0.3
    return watcher, grok, broadcast


def _fail_if_called(_text: str) -> bool:
    raise AssertionError("the bound session send carries mngr's refusal kind, so the flush hook is not used")


def _held(watcher: GrokSessionWatcher) -> list[tuple[str, bool]]:
    return [(str(entry["content"]), bool(entry["is_sending"])) for entry in watcher.get_queued_messages()]


def test_the_flush_never_types_while_a_top_level_turn_or_a_pending_wake_is_in_flight(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    watcher._queue.enqueue("also fix the tests", "t0", is_turn_open=False)

    write_root_turn(tmp_path, _PLAIN_ROOT, "prompt-9")
    watcher._attempt_flush()
    write_root_turn(tmp_path, _PLAIN_ROOT, f"pending-wake:{_BACKGROUND_SUBAGENT_CHILD}")
    watcher._attempt_flush()

    assert grok.sent == []
    assert _held(watcher) == [("also fix the tests", False)], "held behind the turn, it reads Queued"


def test_the_flush_delivers_the_held_block_once_the_turn_ends_and_witnesses_it_in_the_parsed_log(
    tmp_path: Path,
) -> None:
    watcher, grok, broadcast = _flushing_watcher(tmp_path)
    write_root_turn(tmp_path, _PLAIN_ROOT, "prompt-9")
    watcher._queue.enqueue("first", "t0", is_turn_open=True)
    watcher._queue.enqueue("second\nwith two lines", "t1", is_turn_open=True)
    remove_root_turn(tmp_path, _PLAIN_ROOT)

    watcher._attempt_flush()

    assert grok.sent == ["first\nsecond\nwith two lines"]
    assert watcher.get_queued_messages() == []
    assert [event["content"] for event in broadcast if event["type"] == "user_message"] == [
        "first\nsecond\nwith two lines"
    ]


def test_a_dead_agent_has_its_messages_held_and_shown_as_queued(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path, is_alive=False)
    watcher._queue.enqueue("stranded", "t0", is_turn_open=False)

    watcher._attempt_flush()

    assert grok.sent == []
    assert _held(watcher) == [("stranded", False)]
    block, taken = watcher.take_unclaimed_queue()
    assert (block, len(taken)) == ("stranded", 1), "stop can still return it to the composer"


def test_a_send_mngr_refuses_as_busy_keeps_the_message_held_without_spending_an_attempt(tmp_path: Path) -> None:
    """A turn that started between the gate and the send is a 'not yet': nothing was typed into grok."""
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    queued_id = watcher._queue.enqueue("held", "t0", is_turn_open=False)
    grok.refusal = SendFailedError("grok is running a turn", "agent_busy")

    for _ in range(4):
        watcher._attempt_flush()

    assert _held(watcher) == [("held", False)]
    assert watcher._queue.is_exhausted(queued_id) is False
    grok.refusal = None
    watcher._attempt_flush()
    assert grok.sent == ["held"]
    assert watcher.get_queued_messages() == []


def test_a_session_send_that_reports_the_text_untaken_keeps_the_message_held(tmp_path: Path) -> None:
    watcher, _grok, _broadcast = _flushing_watcher(tmp_path)
    watcher._queue.bind_session_send(lambda _text: False)
    watcher._queue.enqueue("held", "t0", is_turn_open=False)

    watcher._attempt_flush()

    assert _held(watcher) == [("held", False)]


def test_a_flush_whose_log_read_fails_leaves_the_message_unclaimed_for_the_next_one(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    watcher._queue.enqueue("held", "t0", is_turn_open=True)
    roots_path = tmp_path / ROOTS_RELATIVE_PATH
    roots_text = roots_path.read_text()
    roots_path.unlink()
    roots_path.mkdir()

    with pytest.raises(OSError):
        watcher._attempt_flush()
    roots_path.rmdir()
    roots_path.write_text(roots_text)
    watcher._attempt_flush()

    assert grok.sent == ["held"]
    assert watcher.get_queued_messages() == []


def test_a_send_mngr_confirmed_is_not_typed_again_when_the_log_read_after_it_fails(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    grok.is_committing = False
    roots_path = tmp_path / ROOTS_RELATIVE_PATH
    roots_text = roots_path.read_text()

    def break_the_roots_file() -> None:
        roots_path.unlink()
        roots_path.mkdir()

    grok.on_send = break_the_roots_file
    watcher._queue.enqueue("confirmed once", "t0", is_turn_open=True)
    with pytest.raises(OSError):
        watcher._attempt_flush()
    is_emit_muted = watcher._emit_embargo_until > time.monotonic()
    grok.on_send = None
    roots_path.rmdir()
    roots_path.write_text(roots_text)
    watcher._attempt_flush()

    assert grok.sent == ["confirmed once"]
    assert watcher.get_queued_messages() == []
    assert not is_emit_muted


def test_a_gate_screen_keeps_the_held_messages_and_retries_them(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    queued_id = watcher._queue.enqueue("held behind a gate", "t0", is_turn_open=True)
    grok.refusal = SendFailedError("grok shows its consent screen", "input_blocked")

    for _ in range(4):
        watcher._attempt_flush()

    assert watcher._queue.is_exhausted(queued_id) is False
    assert _held(watcher) == [("held behind a gate", False)]


def test_a_send_mngr_confirmed_is_never_typed_again_though_grok_logs_no_turn_for_it(tmp_path: Path) -> None:
    """mngr confirmed grok took it, so typing it again would deliver it twice."""
    watcher, grok, broadcast = _flushing_watcher(tmp_path)
    watcher._queue.enqueue("taken by grok", "t0", is_turn_open=True)
    grok.is_committing = False

    for _ in range(3):
        watcher._attempt_flush()

    assert grok.sent == ["taken by grok"]
    assert watcher.get_queued_messages() == []
    assert watcher._emit_embargo_until == 0.0


def test_a_message_grok_parked_behind_a_turn_started_inside_mngrs_send_is_typed_once(tmp_path: Path) -> None:
    """A background shell's follow-up turn can start between mngr's busy check and its paste, and grok runs the
    paste only after that turn, however long it takes."""
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    grok.is_committing = False
    grok.on_send = lambda: write_root_turn(tmp_path, _PLAIN_ROOT, "task-completed-shell-2")
    watcher._queue.enqueue("parked by grok", "t0", is_turn_open=True)

    watcher._attempt_flush()
    grok.on_send = None
    remove_root_turn(tmp_path, _PLAIN_ROOT)
    grok.commit("parked by grok")
    grok.is_committing = True
    watcher._attempt_flush()

    assert grok.sent == ["parked by grok"]
    assert watcher.get_queued_messages() == []


def test_a_message_typed_after_a_draft_left_in_grok_s_composer_is_delivered_once(tmp_path: Path) -> None:
    """Measured live: mngr types after the draft, finds no evidence for the message alone, and grok logs both."""
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    grok.draft = "DRAFT-by-human "
    grok.is_unconfirmed = True
    watcher._queue.enqueue("after a human draft", "t0", is_turn_open=True)

    watcher._attempt_flush()
    grok.is_unconfirmed = False
    watcher._attempt_flush()

    assert grok.sent == ["after a human draft"]
    assert watcher.get_queued_messages() == []


def test_a_send_mngr_failed_unclassified_after_typing_is_not_typed_again_once_grok_logs_it(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)

    def fail_after_typing() -> None:
        raise SendFailedError("tmux send-keys Enter failed", "unknown")

    grok.on_send = fail_after_typing
    watcher._queue.enqueue("typed once", "t0", is_turn_open=True)

    watcher._attempt_flush()
    grok.on_send = None
    watcher._attempt_flush()

    assert grok.sent == ["typed once"]
    assert watcher.get_queued_messages() == []


def test_a_send_mngr_could_not_confirm_and_grok_logged_no_turn_for_spends_an_attempt(tmp_path: Path) -> None:
    watcher, grok, broadcast = _flushing_watcher(tmp_path)
    queued_id = watcher._queue.enqueue("never logged", "t0", is_turn_open=True)
    grok.is_committing = False
    grok.is_unconfirmed = True

    watcher._attempt_flush()

    assert grok.sent == ["never logged"]
    assert _held(watcher) == [("never logged", False)]
    assert watcher._queue.is_exhausted(queued_id) is False
    assert watcher._emit_embargo_until == 0.0
    append_grok_updates(
        tmp_path, _PLAIN_ROOT, _committed_turn_lines(_PLAIN_ROOT, "typed at the pane", 7, 1792600000000)
    )
    watcher._emit_cycle()
    assert "typed at the pane" in [event.get("content") for event in broadcast]


def test_a_foreign_turn_does_not_resolve_the_held_block(tmp_path: Path) -> None:
    """A turn someone typed at the pane during the send is not the block: only its text identifies it."""
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    watcher._queue.enqueue("mine", "t0", is_turn_open=True)
    grok.is_committing = False
    grok.is_unconfirmed = True
    grok.on_send = lambda: append_grok_updates(
        tmp_path, _PLAIN_ROOT, _committed_turn_lines(_PLAIN_ROOT, "someone else's", 5, 1792600000000)
    )

    watcher._attempt_flush()

    assert _held(watcher) == [("mine", False)]


def test_the_held_messages_chip_departs_before_its_turn_arrives_even_when_the_live_emit_runs_mid_send(
    tmp_path: Path,
) -> None:
    """Contract A3b. The live emit wakes on grok's own write, so it is forced to run inside the send."""
    watcher, grok, broadcast = _flushing_watcher(tmp_path)
    order: list[str] = []
    watcher.set_queue_snapshot_callback(lambda snapshot: order.append("queue-present" if snapshot else "queue-empty"))

    def record_transcript(_agent_id: str, events: list[dict[str, Any]]) -> None:
        order.append("transcript")
        broadcast.extend(events)

    watcher._on_events = record_transcript
    grok.on_send = watcher._emit_cycle
    watcher._queue.enqueue("deliver me", "t0", is_turn_open=False)
    order.clear()

    watcher._attempt_flush()

    assert grok.sent == ["deliver me"]
    assert order.index("queue-empty") < order.index("transcript"), order
    assert [event["content"] for event in broadcast if event["type"] == "user_message"] == ["deliver me"]


def test_a_live_emit_that_wakes_while_a_flush_broadcasts_its_held_events_cannot_go_first(tmp_path: Path) -> None:
    watcher, _grok, _broadcast = _flushing_watcher(tmp_path)
    user_turns: list[str] = []
    broadcast_count = [0]

    def record_transcript(_agent_id: str, events: list[dict[str, Any]]) -> None:
        broadcast_count[0] += 1
        if broadcast_count[0] == 1:
            append_grok_updates(
                tmp_path, _PLAIN_ROOT, _committed_turn_lines(_PLAIN_ROOT, "typed later", 7, 1792600000000)
            )
            watcher._emit_cycle()
        user_turns.extend(str(event["content"]) for event in events if event["type"] == "user_message")

    watcher._on_events = record_transcript
    watcher._queue.enqueue("deliver me", "t0", is_turn_open=False)

    watcher._attempt_flush()
    watcher._emit_cycle()

    assert user_turns == ["deliver me", "typed later"]


def test_a_control_command_goes_out_alone_ahead_of_the_held_block_and_is_never_a_chip(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    grok.is_committing = False
    published: list[list[str]] = []
    watcher.set_queue_snapshot_callback(lambda snapshot: published.append([str(e["content"]) for e in snapshot]))
    watcher._queue.enqueue("hello", "t0", is_turn_open=True)
    watcher._queue.enqueue_control_command("/model grok-4.6")
    watcher._queue.enqueue_control_command("/effort low")

    watcher._attempt_flush()
    watcher._attempt_flush()
    grok.is_committing = True
    watcher._attempt_flush()

    assert grok.sent == ["/model grok-4.6", "/effort low", "hello"]
    assert all(not any(content.startswith("/") for content in snapshot) for snapshot in published)
    assert watcher.get_queued_messages() == []


def test_a_control_command_waits_out_a_turn_like_a_message(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    write_root_turn(tmp_path, _PLAIN_ROOT, "prompt-9")
    watcher._queue.enqueue_control_command("/model grok-4.5")

    watcher._attempt_flush()
    assert grok.sent == []

    remove_root_turn(tmp_path, _PLAIN_ROOT)
    grok.is_committing = False
    watcher._attempt_flush()
    assert grok.sent == ["/model grok-4.5"]
    assert watcher._queue.next_control_command() is None


def test_a_control_command_mngr_refuses_for_a_gate_answers_the_send_waiting_behind_it(tmp_path: Path) -> None:
    """The message meets the same gate, so its POST fails with mngr's words rather than waiting out the gate."""
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    grok.refusal = SendFailedError("grok shows its consent screen", "input_blocked")
    watcher._queue.enqueue_control_command("/model grok-4.5")
    queued_id = watcher._queue.enqueue("hello", "t0", is_turn_open=False)

    watcher._attempt_flush()

    result = watcher._queue.wait_for_send_result(queued_id, 0.0)
    assert result is not None
    assert (result.verdict, result.reason) == (GrokSendVerdict.BLOCKED, "grok shows its consent screen")
    assert _held(watcher) == [("hello", False)]
    assert watcher._queue.next_control_command() == "/model grok-4.5"
    assert grok.sent == []


@pytest.mark.parametrize(
    ("command", "model_state"),
    [
        ("/effort high", {"model": "grok-4.5", "effort": "high", "fast": False}),
        ("/model grok-4.6", {"model": "grok-4.6", "effort": "xhigh", "fast": False}),
    ],
    ids=["effort", "model"],
)
def test_a_control_command_grok_already_runs_is_dropped_unsent(
    tmp_path: Path, command: str, model_state: dict[str, Any]
) -> None:
    """mngr confirms a switch by model_state.json changing, which it never does for one already in force."""
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    get_grok_model_state_path(tmp_path).write_text(json.dumps(model_state) + "\n")
    watcher._queue.enqueue_control_command(command)

    watcher._attempt_flush()

    assert grok.sent == []
    assert watcher._queue.next_control_command() is None
    assert watcher._flush_wake.is_set()


def test_a_control_command_grok_does_not_run_yet_is_sent(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    grok.is_committing = False
    state = {"model": "grok-4.6", "effort": "high", "fast": False}
    get_grok_model_state_path(tmp_path).write_text(json.dumps(state) + "\n")
    watcher._queue.enqueue_control_command("/effort low")

    watcher._attempt_flush()

    assert grok.sent == ["/effort low"]


def test_a_slash_command_is_sent_alone_once_and_never_typed_again(tmp_path: Path) -> None:
    """Measured live: grok runs `/timestamps` or `/new` itself and logs no user turn for it."""
    watcher, grok, broadcast = _flushing_watcher(tmp_path)
    watcher._queue.enqueue("before", "t0", is_turn_open=True)
    watcher._queue.enqueue("/new", "t1", is_turn_open=True)
    watcher._queue.enqueue("after", "t2", is_turn_open=True)

    def run_locally(text: str) -> bool:
        if text.startswith("/"):
            grok.sent.append(text)
            return True
        return grok.send(text)

    watcher._queue.bind_session_send(run_locally)
    for _ in range(5):
        watcher._attempt_flush()

    assert grok.sent == ["before", "/new", "after"]
    assert watcher.get_queued_messages() == []
    assert [event["content"] for event in broadcast if event["type"] == "user_message"] == ["before", "after"]


def test_a_grok_relaunch_drops_the_held_messages_before_anything_is_sent_into_it(tmp_path: Path) -> None:
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    watcher._queue.enqueue("from the old process", "t0", is_turn_open=True)
    (tmp_path / PROCESS_STARTED_MARKER_FILENAME).write_text("")

    watcher._attempt_flush()

    assert grok.sent == []
    assert watcher.get_queued_messages() == []


def test_without_a_bound_session_send_an_untaken_hook_send_grok_ran_is_not_typed_again(tmp_path: Path) -> None:
    """The flush hook answers only whether mngr confirmed the send, so its False may still have typed the text."""
    watcher, grok, _broadcast = _flushing_watcher(tmp_path)
    watcher._queue.unbind_session_send(grok.send)
    assert watcher._queue.get_session_send() is None
    hook_sent: list[str] = []

    def unconfirmed_hook(text: str) -> bool:
        hook_sent.append(text)
        grok.commit(text)
        write_root_turn(tmp_path, _PLAIN_ROOT, "prompt-9")
        return False

    watcher.set_flush_hooks(unconfirmed_hook, lambda: True)
    watcher._queue.enqueue("held", "t0", is_turn_open=True)

    watcher._attempt_flush()
    remove_root_turn(tmp_path, _PLAIN_ROOT)
    watcher._attempt_flush()

    assert hook_sent == ["held"]
    assert watcher.get_queued_messages() == []


def test_a_root_turn_change_wakes_the_worker_only_while_something_is_held(tmp_path: Path) -> None:
    watcher, _grok, _broadcast = _flushing_watcher(tmp_path)

    watcher._on_root_turns_changed()
    assert not watcher._flush_wake.is_set()

    watcher._queue.enqueue_control_command("/effort high")
    watcher._on_root_turns_changed()
    assert not watcher._flush_wake.is_set()

    write_root_turn(tmp_path, _PLAIN_ROOT, "prompt-9")
    watcher._on_root_turns_changed()
    assert watcher._flush_wake.is_set()


def test_the_running_worker_sends_a_held_message_when_the_turn_ends_without_waiting_for_its_retry(
    tmp_path: Path,
) -> None:
    """The root-turn directory wakes the worker: the transcript's idle edge can come before mngr clears the turn."""
    write_grok_agent_state(tmp_path, find_fixture_scenario("plain"))
    write_root_turn(tmp_path, _PLAIN_ROOT, "prompt-9")
    watcher, _broadcast, _reported = _build_watcher(tmp_path)
    grok = _ScriptedGrok(tmp_path)
    sent_at: list[float] = []
    grok.on_send = lambda: sent_at.append(time.monotonic())
    publish_count = [0]
    watcher.set_queue_snapshot_callback(lambda _snapshot: publish_count.__setitem__(0, publish_count[0] + 1))
    watcher.set_flush_hooks(_fail_if_called, lambda: True)
    watcher._queue.bind_session_send(grok.send)
    watcher.delivery_witness_seconds = 0.3
    watcher.start()
    try:
        watcher._queue.enqueue("held", "t0", is_turn_open=True)
        # The enqueue publishes the queue and each attempt republishes it; one attempt has found the turn in flight.
        wait_for(lambda: publish_count[0] >= 2, timeout=5.0, poll_interval=0.02)
        assert grok.sent == []
        turn_ended_at = time.monotonic()
        remove_root_turn(tmp_path, _PLAIN_ROOT)
        wait_for(lambda: bool(sent_at), timeout=3.0, poll_interval=0.02)
    finally:
        watcher.stop()

    assert grok.sent == ["held"]
    assert sent_at[0] - turn_ended_at < _SOONER_THAN_THE_RETRY_SECONDS


def test_a_loader_holds_no_queue_for_the_agent(tmp_path: Path) -> None:
    write_grok_agent_state(tmp_path, find_fixture_scenario("plain"))

    GrokTranscriptLoader.build_loader(_agent_info(tmp_path)).get_all_events()

    assert tmp_path.name not in queue_tracker._TRACKERS
