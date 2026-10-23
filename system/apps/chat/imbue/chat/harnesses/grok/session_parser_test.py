"""Tests for the grok session parser, run end to end over grok 1.0.62's own session logs."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from imbue.chat.compaction_status import is_context_compacted_event
from imbue.chat.harnesses.grok.session_parser import GrokLaneState
from imbue.chat.harnesses.grok.session_parser import INTERNAL_MARKER_TYPES
from imbue.chat.harnesses.grok.session_parser import SUBAGENT_SPAWNED_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import TASK_FINISHED_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import THINKING_SOURCE_MARKER_TYPE
from imbue.chat.harnesses.grok.session_parser import detail_for
from imbue.chat.harnesses.grok.session_parser import parse_record
from imbue.chat.harnesses.grok.session_parser import record_timestamp
from imbue.chat.harnesses.grok.session_parser import unwrap_send_now
from imbue.chat.harnesses.grok.testing import find_fixture_scenario
from imbue.chat.harnesses.grok.testing import list_fixture_scenarios
from imbue.chat.harnesses.transcript_store import TranscriptStore
from imbue.chat.harnesses.transcript_store import iter_line_spans
from imbue.imbue_common.frozen_model import FrozenModel

_ALL_SCENARIOS = list_fixture_scenarios()
_LANE = "lane"
_SUBAGENT_ENDS_MID_REPLY_ROOT = "5658a8e7-b909-48b5-b230-c5499cc4a3e1"
_SUBAGENT_ENDS_MID_REPLY_CHILD = "01a16809-3142-7c61-82ac-f493c95a71d0"


class ParsedSession(FrozenModel):
    """One fixture session parsed the way its loader parses it, through a transcript store."""

    session_id: str = Field(description="The parsed session")
    records_by_offset: dict[int, dict[str, Any]] = Field(description="Each record by its byte offset")
    events: list[dict[str, Any]] = Field(description="The wire events in store order, superseded in place")
    markers: list[dict[str, Any]] = Field(description="The records parse_record returned for the loader")
    source_offset_by_event_id: dict[str, int] = Field(description="The offset of the record each event came from")

    def record_of(self, event: dict[str, Any]) -> dict[str, Any]:
        return self.records_by_offset[self.source_offset_by_event_id[event["event_id"]]]


def _parse_session(scenario_dir: Path, session_id: str) -> ParsedSession:
    records_by_offset: dict[int, dict[str, Any]] = {}
    markers: list[dict[str, Any]] = []
    source_offset_by_event_id: dict[str, int] = {}
    roots = scenario_dir.joinpath("roots").read_text().split()
    lane = GrokLaneState(session_id=session_id, is_root=session_id in roots)
    store = TranscriptStore.build()
    path = scenario_dir / session_id / "updates.jsonl"
    for offset, length, line in iter_line_spans(path.read_bytes(), 0):
        if not line.strip():
            continue
        record = json.loads(line)
        records_by_offset[offset] = record
        for event in parse_record(record, lane, offset=offset):
            if event["type"] in INTERNAL_MARKER_TYPES:
                markers.append(event)
                continue
            store.ingest(_LANE, event, (path, offset, length))
            source_offset_by_event_id[event["event_id"]] = offset
    return ParsedSession(
        session_id=session_id,
        records_by_offset=records_by_offset,
        events=store.all_events([_LANE]),
        markers=markers,
        source_offset_by_event_id=source_offset_by_event_id,
    )


def _parse(scenario: str, session_id: str | None = None) -> ParsedSession:
    scenario_dir = find_fixture_scenario(scenario)
    chosen = session_id or scenario_dir.joinpath("roots").read_text().split()[0]
    return _parse_session(scenario_dir, chosen)


def _records(parsed: ParsedSession) -> list[dict[str, Any]]:
    """A copy of the session's records, in the order grok wrote them."""
    return [json.loads(json.dumps(parsed.records_by_offset[offset])) for offset in sorted(parsed.records_by_offset)]


def _record_of_kind(parsed: ParsedSession, kind: str) -> dict[str, Any]:
    """A copy of the session's first record of ``kind``."""
    return next(record for record in _records(parsed) if record["params"]["update"]["sessionUpdate"] == kind)


def _text_line_index(records: list[dict[str, Any]], text_start: str) -> int:
    """The index of the first assistant text line whose text starts with ``text_start``."""
    return next(
        index
        for index, record in enumerate(records)
        if record["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
        and record["params"]["update"]["content"]["text"].startswith(text_start)
    )


def _summary(event: dict[str, Any]) -> tuple[Any, ...]:
    """What a user sees of one event, compactly."""
    match event["type"]:
        case "special":
            return (event["kind"],)
        case "user_message":
            if "display" in event:
                return (event["display"], event.get("display_label"), event.get("display_body"))
            return ("user", event["content"])
        case "assistant_message":
            if event["tool_calls"]:
                tool_call = event["tool_calls"][0]
                return ("call", tool_call["tool_name"], tool_call["caption_label"])
            if event["stop_reason"] == "error":
                return ("error", event["text"])
            return ("text", event["text"][:40])
        case "tool_result":
            return ("result", event["tool_name"], event["is_error"], event.get("error_snippet"))
        case _ as other:
            raise AssertionError(f"unexpected event type {other}")


def _summaries(parsed: ParsedSession) -> list[tuple[Any, ...]]:
    return [_summary(event) for event in parsed.events]


def _wire_events(session_id: str, records: list[dict[str, Any]], is_root: bool) -> list[dict[str, Any]]:
    lane = GrokLaneState(session_id=session_id, is_root=is_root)
    events: list[dict[str, Any]] = []
    for offset, record in enumerate(records):
        events.extend(parse_record(record, lane, offset=offset))
    return events


def test_plain_turn_reads_as_the_users_prompt_then_the_reply() -> None:
    assert _summaries(_parse("plain")) == [
        ("turn_started",),
        ("user", "hello plain"),
        ("text", "STUB_REPLY: hello from the stub model k="),
        ("turn_completed",),
    ]


def test_a_tool_turn_pairs_the_call_with_its_result_by_the_calls_offset() -> None:
    parsed = _parse("tool")

    assert _summaries(parsed) == [
        ("turn_started",),
        ("user", "TOOLTEST please"),
        ("call", "run_terminal_command", "Running write marker file"),
        ("result", "run_terminal_command", False, None),
        ("text", "STUB_REPLY: tool finished"),
        ("turn_completed",),
    ]
    call_event, result_event = parsed.events[2], parsed.events[3]
    tool_call = call_event["tool_calls"][0]
    assert call_event["event_id"] == f"grok-{parsed.session_id}-1194-call"
    assert tool_call["tool_call_id"] == call_event["event_id"]
    assert tool_call["header_label"] == "Tool: Bash"
    assert result_event["event_id"] == f"grok-{parsed.session_id}-1194-result"
    assert result_event["tool_call_id"] == tool_call["tool_call_id"]
    assert result_event["output_chars"] == len("exit: 0\ndone\n")
    assert call_event["model"] == "stub-model"
    assert {event["session_id"] for event in parsed.events} == {parsed.session_id}


def test_turn_markers_carry_isoformat_times_and_the_turns_prompt_id() -> None:
    parsed = _parse("tool")

    assert parsed.events[0]["timestamp"] == "2026-10-20T01:07:13.731Z"
    assert parsed.events[-1]["turn_id"] == "5afcb831-3a33-4520-b719-73a13ea3adcd"
    assert parsed.events[-1]["event_id"] == f"grok-{parsed.session_id}-6025-turn-completed"


def test_a_cancel_aborts_the_turn_and_answers_the_killed_tool_as_interrupted() -> None:
    parsed = _parse("interrupt")

    assert _summaries(parsed) == [
        ("turn_started",),
        ("user", "SLOWTEST interrupt me while streaming"),
        ("text", "STUB_REPLY: slow one two"),
        ("turn_aborted",),
        ("turn_started",),
        ("user", "SLOWTOOL interrupt me during the tool"),
        ("call", "run_terminal_command", "Running slow foreground sleep"),
        ("result", "run_terminal_command", True, "Interrupted."),
        ("turn_aborted",),
        ("turn_started",),
        ("user", "hello after interrupts"),
        ("text", "STUB_REPLY: hello from the stub model k="),
        ("turn_completed",),
    ]
    interrupted = parsed.events[7]
    assert interrupted["output_chars"] == 0
    assert interrupted["tool_call_id"] == parsed.events[6]["tool_calls"][0]["tool_call_id"]


def test_a_terminal_update_for_the_killed_tool_written_after_the_cancel_leaves_it_interrupted() -> None:
    """grok writes none for a call a cancel killed; one after the turn's end belongs to no call of an open turn."""
    records = _records(_parse("interrupt"))
    cancel_index = next(
        index
        for index, record in enumerate(records)
        if record["params"]["update"].get("prompt_id") == "1047385a-6884-4d0c-b2d2-ebdfa8251fb7"
        and record["params"]["update"]["sessionUpdate"] == "turn_completed"
    )
    finished = json.loads(json.dumps(_parse("tool").records_by_offset[3977]))
    finished["params"]["update"]["toolCallId"] = "call_stub_3"

    results = [
        event
        for event in _wire_events("s", [*records[: cancel_index + 1], finished, *records[cancel_index + 1 :]], True)
        if event["type"] == "tool_result"
    ]

    assert [(event["is_error"], event["error_snippet"]) for event in results] == [(True, "Interrupted.")]


def test_a_second_terminal_update_for_a_call_adds_no_second_result() -> None:
    records = _records(_parse("tool"))
    finished_index = next(
        index for index, record in enumerate(records) if record["params"]["update"].get("status") == "completed"
    )
    failed_again = json.loads(json.dumps(records[finished_index]))
    failed_again["params"]["update"]["status"] = "failed"

    events = _wire_events("s", [*records[: finished_index + 1], failed_again, *records[finished_index + 1 :]], True)

    results = [event for event in events if event["type"] == "tool_result"]
    assert [(result["tool_name"], result["is_error"]) for result in results] == [("run_terminal_command", False)]


def test_a_send_now_follow_up_is_a_turn_of_its_own_after_the_aborted_one() -> None:
    assert _summaries(_parse("send_now")) == [
        ("turn_started",),
        ("user", "SLOWTEST first long turn"),
        ("text", "STUB_REPLY: slow one two three"),
        ("turn_aborted",),
        ("turn_started",),
        ("user", "queued follow up"),
        ("text", "STUB_REPLY: hello from the stub model k="),
        ("turn_completed",),
    ]


def test_a_blocking_stop_hook_shows_as_a_chip_inside_the_turn_it_continued() -> None:
    feedback = ("chip", "Stop hook feedback", "STOPGATE_FEEDBACK please continue once more")
    parsed = _parse("stop_block")

    assert _summaries(parsed) == [
        ("turn_started",),
        ("user", "hello block one"),
        ("text", "STUB_REPLY: hello from the stub model k="),
        feedback,
        ("text", "STUB_REPLY: hello from the stub model k="),
        ("turn_completed",),
        ("turn_started",),
        ("user", "BGTEST block two"),
        ("call", "run_terminal_command", "Running background sleep"),
        ("result", "run_terminal_command", False, None),
        ("text", "STUB_REPLY: tool finished"),
        feedback,
        ("text", "STUB_REPLY: hello from the stub model k="),
        ("turn_completed",),
        ("turn_started",),
        ("notice", "Background task completed", "background sleep"),
        ("text", "STUB_WAKE_REPLY: acknowledged background"),
        feedback,
        ("text", "STUB_REPLY: hello from the stub model k="),
        ("turn_completed",),
    ]
    assert {event["role"] for event in parsed.events if event.get("display") == "chip"} == {"system"}


def test_a_background_shells_wake_turn_is_a_notice_and_its_end_is_reported() -> None:
    parsed = _parse("background_shell")

    assert _summaries(parsed) == [
        ("turn_started",),
        ("user", "BGTEST run something in the background"),
        ("call", "run_terminal_command", "Running background sleep"),
        ("result", "run_terminal_command", False, None),
        ("text", "STUB_REPLY: tool finished"),
        ("turn_completed",),
        ("turn_started",),
        ("notice", "Background task completed", "background sleep"),
        ("text", "STUB_WAKE_REPLY: acknowledged background"),
        ("turn_completed",),
    ]
    wake = parsed.events[7]
    assert wake["role"] == "user"
    assert "non_turn_tail" not in wake
    assert parsed.events[-1]["turn_id"] == "task-completed-01a156da-5fa8-7e01-bed9-b3e2112a053a"
    assert parsed.markers == [
        {"type": TASK_FINISHED_MARKER_TYPE, "task_id": "01a156da-5fa8-7e01-bed9-b3e2112a053a", "will_wake": True}
    ]


def test_a_background_subagent_links_its_child_session_to_the_spawn_call() -> None:
    root_id = "01a156db-07ea-7993-b2e6-dcaa7cb0a81c"
    child_id = "01a156db-10ae-70c2-9a0f-a0dff6d5f376"
    parsed = _parse("background_subagent", root_id)

    assert _summaries(parsed) == [
        ("turn_started",),
        ("user", "SUBAGENTBG spawn one in the background"),
        ("call", "spawn_subagent", "Delegating to sub-agent…"),
        ("result", "spawn_subagent", False, None),
        ("text", "STUB_REPLY: tool finished"),
        ("turn_completed",),
        ("turn_started",),
        ("notice", "Background subagent completed", '"stub subagent bg" completed successfully'),
        ("text", "STUB_REPLY: hello from the stub model k="),
        ("turn_completed",),
    ]
    spawn_call = parsed.events[2]["tool_calls"][0]
    assert spawn_call["header_label"] == "Tool: Agent"
    assert spawn_call["description"] == "stub subagent bg"
    assert parsed.markers == [
        {
            "type": SUBAGENT_SPAWNED_MARKER_TYPE,
            "child_session_id": child_id,
            "call_event_id": spawn_call["tool_call_id"],
            "subagent_metadata": {
                "agent_type": "general-purpose",
                "description": "stub subagent bg",
                "session_id": child_id,
            },
        },
        {"type": TASK_FINISHED_MARKER_TYPE, "task_id": child_id, "will_wake": True},
    ]


def test_a_child_session_shows_its_own_conversation_and_marks_only_its_turns_end() -> None:
    """A child's turn_started would read as the chat's own turn; its end settles the subagent's view."""
    child_id = "01a156db-10ae-70c2-9a0f-a0dff6d5f376"
    parsed = _parse("background_subagent", child_id)

    assert _summaries(parsed) == [
        ("user", "say hi from the subagent"),
        ("text", "STUB_CHILD: slow child one two three fou"),
        ("turn_completed",),
    ]
    assert {event["session_id"] for event in parsed.events} == {child_id}


def test_a_wake_turn_whose_model_stays_silent_ends_on_an_error_message() -> None:
    parsed = _parse("failed_wake")

    assert _summaries(parsed)[-4:] == [
        ("turn_started",),
        ("notice", "Background task completed", "background sleep"),
        ("error", "empty response from model (no_visible_content)"),
        ("turn_completed",),
    ]
    error = parsed.events[-2]
    assert error["is_api_error"] is True
    assert error["is_auth_error"] is False
    assert error["api_error_kind"] is None
    assert error["event_id"] == f"grok-{parsed.session_id}-17261-error"


def test_a_foreground_subagent_turn_links_its_child_and_a_reused_tool_call_id_gets_a_new_id() -> None:
    root_id = "6def8bc3-a90e-47b9-9511-9dd4240aafdc"
    parsed = _parse("foreground_subagent_failing_hooks", root_id)

    calls = [
        event["tool_calls"][0]
        for event in parsed.events
        if event["type"] == "assistant_message" and event["tool_calls"]
    ]
    results = [event for event in parsed.events if event["type"] == "tool_result"]
    assert [(call["tool_name"], call["tool_call_id"]) for call in calls] == [
        ("run_terminal_command", f"grok-{root_id}-3947-call"),
        ("spawn_subagent", f"grok-{root_id}-10538-call"),
        ("run_terminal_command", f"grok-{root_id}-20822-call"),
    ]
    assert [result["tool_call_id"] for result in results] == [call["tool_call_id"] for call in calls]
    assert [marker["type"] for marker in parsed.markers] == [SUBAGENT_SPAWNED_MARKER_TYPE, TASK_FINISHED_MARKER_TYPE]
    assert parsed.markers[0]["call_event_id"] == f"grok-{root_id}-10538-call"
    assert [summary for summary in _summaries(parsed) if summary[0] in {"turn_started", "turn_completed"}] == [
        ("turn_started",),
        ("turn_completed",),
    ] * 5


def test_two_root_sessions_each_parse_into_their_own_turns() -> None:
    resumed = _parse("new_session_and_resume", "f603168f-7fec-4427-9b3f-f621a162e742")
    opened_by_new = _parse("new_session_and_resume", "01a156dc-9ff8-7e32-a636-a71e85c2dfee")

    assert _summaries(resumed) == [
        ("turn_started",),
        ("user", "first prompt in N2"),
        ("text", "STUB_REPLY: hello from the stub model nu"),
        ("turn_completed",),
        ("turn_started",),
        ("user", "second prompt in N2 after resume"),
        ("text", "STUB_REPLY: hello from the stub model nu"),
        ("turn_completed",),
    ]
    assert _summaries(opened_by_new) == [
        ("turn_started",),
        ("user", "prompt in the /new session of N2"),
        ("text", "STUB_REPLY: hello from the stub model nu"),
        ("turn_completed",),
    ]
    assert not {event["event_id"] for event in resumed.events} & {event["event_id"] for event in opened_by_new.events}


def test_a_turn_a_quit_cut_short_is_aborted_on_resume_with_its_tool_interrupted_and_a_note() -> None:
    """grok closes the turn an exited process left open when ``--resume`` reloads the session."""
    parsed = _parse("resume_after_quit")

    assert _summaries(parsed) == [
        ("turn_started",),
        ("user", "SCEN:slowtool go"),
        ("call", "run_terminal_command", "Running long sleep"),
        ("result", "run_terminal_command", True, "Interrupted."),
        ("chip", "Turn interrupted", None),
        ("turn_aborted",),
        ("turn_started",),
        ("user", "hello after resume"),
        ("text", "STUB_REPLY: scenario slowtool finished"),
        ("turn_completed",),
    ]
    note = parsed.events[4]
    assert note["role"] == "system"
    assert note["content"] == parsed.record_of(note)["params"]["update"]["agent_result"]
    assert parsed.events[3]["tool_call_id"] == parsed.events[2]["tool_calls"][0]["tool_call_id"]


def test_a_note_on_an_interrupted_turn_that_no_detector_names_is_not_shown() -> None:
    records = _records(_parse("resume_after_quit"))
    for record in records:
        if record["params"]["update"].get("stop_reason") == "interrupted":
            record["params"]["update"]["agent_result"] = "Something else happened."

    events = _wire_events("s", records, True)

    assert [event["type"] for event in events if event["type"] == "user_message"] == ["user_message"] * 2
    assert [event["kind"] for event in events if event["type"] == "special"][:2] == ["turn_started", "turn_aborted"]


def test_a_call_a_workspace_guard_refused_is_failed_without_a_snippet() -> None:
    """The guard's message is the workspace steering the agent; the template's own policy hook refused these."""
    results = [_summary(event) for event in _parse("guard_refusals").events if event["type"] == "tool_result"]

    assert results == [
        ("result", "run_terminal_command", True, None),
        ("result", "run_terminal_command", True, None),
        ("result", "run_terminal_command", False, None),
        ("result", "run_terminal_command", False, None),
        ("result", "write", True, None),
        ("result", "monitor", True, None),
        ("result", "workflow", True, None),
        ("result", "read_file", False, None),
    ]


def _records_returning_markers(parsed: ParsedSession, marker_type: str) -> list[tuple[int, str, dict[str, Any]]]:
    """Each marker of ``marker_type`` the session's records return, with the offset and kind of its record."""
    lane = GrokLaneState(session_id=parsed.session_id, is_root=True)
    found: list[tuple[int, str, dict[str, Any]]] = []
    for offset, record in sorted(parsed.records_by_offset.items()):
        for event in parse_record(record, lane, offset=offset):
            if event["type"] == marker_type:
                found.append((offset, record["params"]["update"]["sessionUpdate"], event))
    return found


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        pytest.param(
            "subagent_ends_mid_reply",
            [(8908, "subagent_finished", _SUBAGENT_ENDS_MID_REPLY_CHILD, True)],
            id="a-subagent-ending-during-a-later-turn",
        ),
        pytest.param(
            "tools",
            [
                (28381, "task_completed", "01a167b8-0b72-7232-8e5a-adc5b8d864cf", False),
                (37570, "task_completed", "01a167b8-13fa-7552-8f62-fad61a50e14e", False),
                (52523, "task_completed", "01a167b8-138d-7a02-9fbe-d755f98795ab", True),
            ],
            id="a-shell-read-whole-a-shell-killed-and-a-monitor",
        ),
    ],
)
def test_the_end_of_background_work_says_whether_grok_starts_a_turn_for_it(
    scenario: str, expected: list[tuple[int, str, str, bool]]
) -> None:
    """A shell the model read to its end or killed starts no turn; a subagent or monitor that ends on its own does."""
    finished = _records_returning_markers(_parse(scenario), TASK_FINISHED_MARKER_TYPE)

    assert [(offset, kind, marker["task_id"], marker["will_wake"]) for offset, kind, marker in finished] == expected


def test_a_wake_prompt_no_detector_names_is_a_notice_of_its_first_line() -> None:
    records = _records(_parse("background_shell"))
    for record in records:
        update = record["params"]["update"]
        if update["sessionUpdate"] == "user_message_chunk" and update["_meta"].get("hideFromScrollback"):
            update["content"]["text"] = "<system-reminder>\nYour scheduled check fired.\n</system-reminder>"

    notices = [event for event in _wire_events("s", records, True) if event.get("display") == "notice"]

    assert [(event["display_label"], event["display_body"]) for event in notices] == [
        ("Background update", "Your scheduled check fired.")
    ]
    assert "non_turn_tail" not in notices[0]


def test_a_send_now_prompt_grok_wrapped_reads_as_the_users_own_words() -> None:
    records = _records(_parse("send_now"))
    for record in records:
        update = record["params"]["update"]
        if update["sessionUpdate"] == "user_message_chunk" and update["content"]["text"] == "queued follow up":
            update["content"]["text"] = (
                "The user sent a message while you were working: <user_query>\nqueued follow up\n</user_query>"
            )

    contents = [event["content"] for event in _wire_events("s", records, True) if event["type"] == "user_message"]

    assert contents == ["SLOWTEST first long turn", "queued follow up"]


def test_a_spawned_child_links_to_the_spawn_call_with_its_description() -> None:
    """Two spawn calls of one turn: the child goes to the one its description names, not the first."""
    parsed = _parse("background_subagent")
    records = _records(parsed)
    spawn_index = next(
        index for index, record in enumerate(records) if record["params"]["update"]["sessionUpdate"] == "tool_call"
    )
    other_spawn = json.loads(json.dumps(records[spawn_index]))
    other_spawn["params"]["update"]["toolCallId"] = "call_other_spawn"
    other_spawn["params"]["update"]["rawInput"]["description"] = "another subagent"

    lane = GrokLaneState(session_id="s", is_root=True)
    spawned: list[dict[str, Any]] = []
    for index, record in enumerate([*records[:spawn_index], other_spawn, *records[spawn_index:]]):
        spawned.extend(
            event
            for event in parse_record(record, lane, offset=index)
            if event["type"] == SUBAGENT_SPAWNED_MARKER_TYPE
        )

    assert [marker["call_event_id"] for marker in spawned] == [f"grok-s-{spawn_index + 1}-call"]


def test_a_shell_command_that_exits_non_zero_is_a_failed_call_showing_its_exit() -> None:
    parsed = _parse("exit_codes")

    results = [_summary(event) for event in parsed.events if event["type"] == "tool_result"]
    assert results == [
        ("result", "run_terminal_command", True, "exit: 3"),
        ("result", "run_terminal_command", True, "exit: 1"),
        ("result", "run_terminal_command", True, "exit: 1"),
        (
            "result",
            "read_file",
            True,
            "Error: /tmp/grok-fixtures/headless/exit_codes/work/missing-file.txt does not exist.",
        ),
        ("result", "run_terminal_command", False, None),
    ]


def test_a_shell_command_killed_by_a_signal_is_a_failed_call() -> None:
    results = [_summary(event) for event in _parse("signal").events if event["type"] == "tool_result"]

    assert results == [("result", "run_terminal_command", True, "exit: killed (signal 15)")]


def test_a_command_moved_to_the_background_reports_its_end_and_its_wake_is_a_notice() -> None:
    parsed = _parse("exit_codes")

    assert _summaries(parsed)[-4:] == [
        ("turn_started",),
        ("notice", "Background task completed", "timeout"),
        ("text", "STUB_REPLY: scenario o3 finished"),
        ("turn_completed",),
    ]
    assert parsed.markers == [{"type": TASK_FINISHED_MARKER_TYPE, "task_id": "call_stub_88312_0", "will_wake": True}]


def test_the_cards_and_the_step_view_read_their_facts_from_grok_shell_results() -> None:
    parsed = _parse("stamps")
    calls = [
        event["tool_calls"][0]
        for event in parsed.events
        if event["type"] == "assistant_message" and event["tool_calls"]
    ]
    results = [event for event in parsed.events if event["type"] == "tool_result"]

    assert [call.get("display") for call in calls] == ["permission_request", "secret_request", None, None]
    assert results[0]["permission_request"]["request_id"] == "req-grok-1"
    assert results[1]["secret_request"] == {
        "request_id": "sec-grok-1",
        "file": "data/.secrets/svc.env",
        "variables": ["API_TOKEN"],
    }
    assert results[2]["tk_stamp"] == (
        "Created gro-step-ab12: Plan the work\nUpdated gro-step-ab12 -> in_progress\ntk-step gro-step-ab12 title: Plan the work"
    )
    assert all(not result["is_error"] for result in results)


@pytest.mark.parametrize(
    ("command", "expected_display", "expected_tk_command"),
    [
        ("tk create --step 'Plan the work'", "hidden", "tk create --step 'Plan the work'"),
        ("cd /code && tk start gro-step-ab12", None, "cd /code && tk start gro-step-ab12"),
        ("ls -la", None, None),
    ],
)
def test_a_tk_lifecycle_shell_call_is_hidden_only_when_pure_and_always_stamped(
    command: str, expected_display: str | None, expected_tk_command: str | None
) -> None:
    call_record = json.loads(json.dumps(_parse("tool").records_by_offset[1194]))
    call_record["params"]["update"]["rawInput"]["command"] = command

    tool_call = _wire_events("s", [call_record], True)[0]["tool_calls"][0]

    assert tool_call.get("display") == expected_display
    assert tool_call.get("tk_command") == expected_tk_command


def test_a_request_echoed_after_output_grok_truncated_is_still_stamped() -> None:
    long_result = [event for event in _parse("stamps").events if event["type"] == "tool_result"][-1]

    assert long_result["permission_request"]["request_id"] == "req-grok-tail"
    assert long_result["output_chars"] > 20000


def test_a_thought_gives_the_next_assistant_event_its_thinking() -> None:
    parsed = _parse("streaming")

    assert _summaries(parsed) == [
        ("turn_started",),
        ("user", "SCEN:o2 go"),
        ("text", "Text before the call in the same respons"),
        ("call", "run_terminal_command", "Running first call"),
        ("result", "run_terminal_command", False, None),
        ("text", "w0000 w0001 w0002 w0003 w0004 w0005 w000"),
        ("turn_completed",),
    ]
    assert [event.get("has_thinking", False) for event in parsed.events if event["type"] == "assistant_message"] == [
        False,
        False,
        True,
    ]
    assert parsed.markers == [{"type": THINKING_SOURCE_MARKER_TYPE}]
    assert len(parsed.events[5]["text"].split()) == 600


def test_every_workspace_tool_reads_in_the_shared_vocabulary() -> None:
    parsed = _parse("tools")
    calls = [
        event["tool_calls"][0]
        for event in parsed.events
        if event["type"] == "assistant_message" and event["tool_calls"]
    ]

    assert [(call["header_label"], call["caption_label"]) for call in calls] == [
        ("Tool: Write", "Writing made.txt"),
        ("Tool: Edit", "Editing made.txt"),
        ("Tool: Edit", "Editing made.txt"),
        ("Tool: Read", "Reading made.txt"),
        ("Tool: Grep", 'Searching "gamma"'),
        ("Tool: List", "Listing ."),
        ("Tool: Bash", "Running bg shell"),
        ("Tool: TaskOutput", "Retrieving results"),
        ("Tool: Monitor", "Monitoring two ticks"),
        ("Tool: Bash", "Running to be killed"),
        ("Tool: TaskStop", "Stopping a background task"),
        ("Tool: ToolSearch", 'Loading tool "notebook"'),
        ("Tool: run_terminal_command", "Running run_terminal_command"),
        ("Tool: Schedule", "Listing scheduled tasks"),
    ]


def test_each_tool_result_carries_the_text_its_record_holds() -> None:
    parsed = _parse("tools")
    details = {
        event["tool_name"]: detail_for(event, parsed.record_of(event))
        for event in parsed.events
        if event["type"] == "tool_result"
    }
    outputs = {tool_name: detail["output"] for tool_name, detail in details.items() if detail is not None}

    assert details["write"] == {
        "inputs_by_tool_call_id": {},
        "output": "The file /tmp/grok-fixtures/headless/tools/work/made.txt has been created.",
        "thinking": None,
    }
    assert outputs.keys() == details.keys()
    assert outputs["read_file"] == "1→alpha"
    assert outputs["grep"].startswith('<workspace_result workspace_path="/tmp/grok-fixtures/headless/tools/work">')
    assert outputs["list_dir"].endswith("  - made.txt\n  - notes.txt")
    assert outputs["get_command_or_subagent_output"] == "bg-out\n"
    assert outputs["kill_command_or_subagent"] == "Task was terminated successfully"
    assert outputs["monitor"] == ""


def test_monitor_reports_wake_grok_as_notices() -> None:
    notices = [_summary(event) for event in _parse("tools").events if event.get("display") == "notice"]

    assert notices == [
        ("notice", "Monitor event", "[two ticks] tick 2"),
        ("notice", "Monitor ended", "two ticks"),
    ]


@pytest.mark.parametrize("scenario_dir", _ALL_SCENARIOS, ids=lambda path: path.name)
def test_every_payload_is_off_the_wire_and_served_whole_from_the_events_own_record(scenario_dir: Path) -> None:
    for session_id in scenario_dir.joinpath("roots").read_text().split():
        parsed = _parse_session(scenario_dir, session_id)
        for event in parsed.events:
            assert not {"output", "thinking"} & event.keys()
            if event["type"] == "tool_result" and event["output_chars"]:
                detail = detail_for(event, parsed.record_of(event))
                assert detail is not None and len(detail["output"]) == event["output_chars"]
            for tool_call in event.get("tool_calls") or ():
                assert not {"input", "input_preview"} & tool_call.keys()
                detail = detail_for(event, parsed.record_of(event))
                assert detail is not None
                assert len(detail["inputs_by_tool_call_id"][tool_call["tool_call_id"]]) == tool_call["input_chars"]


def test_the_thought_line_serves_the_thinking_of_the_event_it_preceded() -> None:
    parsed = _parse("streaming")
    thought = next(
        record
        for record in parsed.records_by_offset.values()
        if record["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
    )

    detail = detail_for(parsed.events[5], thought)

    assert detail == {"inputs_by_tool_call_id": {}, "output": None, "thinking": "thinking about it " * 20}


def test_a_record_that_holds_none_of_an_events_payloads_gives_no_detail() -> None:
    parsed = _parse("tool")
    user_record = parsed.record_of(parsed.events[1])

    assert detail_for(parsed.events[2], user_record) is None
    assert detail_for(parsed.events[4], parsed.record_of(parsed.events[4])) is None


def test_records_with_nothing_to_show_produce_nothing() -> None:
    parsed = _parse("failed_wake")
    shown_kinds = {
        "user_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "turn_completed",
        "task_completed",
    }
    silent = [
        record
        for record in parsed.records_by_offset.values()
        if record["params"]["update"]["sessionUpdate"] not in shown_kinds
    ]

    assert {record["params"]["update"]["sessionUpdate"] for record in silent} == {
        "hook_execution",
        "task_backgrounded",
        "background_tasks",
        "retry_state",
    }
    assert _wire_events("s", silent, True) == []


def test_a_hook_note_the_detectors_do_not_name_is_not_shown() -> None:
    annotation = json.loads(json.dumps(_parse("stop_block").records_by_offset[2376]))
    annotation["params"]["update"]["message"] = "PostToolUse hook said something"

    assert _wire_events("s", [annotation], True) == []


def test_chunks_of_one_prompt_are_one_user_message_that_grows_in_place() -> None:
    first = _parse("plain").records_by_offset[839]
    second = json.loads(json.dumps(first))
    second["params"]["update"]["content"]["text"] = " and more"

    events = _wire_events("s", [first, second], True)

    assert [(event["type"], event["event_id"]) for event in events] == [
        ("special", "grok-s-0-turn-started"),
        ("user_message", "grok-s-0-user"),
        ("user_message", "grok-s-0-user"),
    ]
    assert events[-1]["content"] == "hello plain and more"


def test_a_child_sessions_cancel_still_answers_its_open_calls() -> None:
    events = _wire_events("child", _records(_parse("interrupt")), False)

    assert [event["error_snippet"] for event in events if event["type"] == "tool_result"] == ["Interrupted."]
    assert [event["kind"] for event in events if event["type"] == "special"] == [
        "turn_aborted",
        "turn_aborted",
        "turn_completed",
    ]


def test_a_reply_split_by_a_subagent_ending_mid_stream_is_one_message() -> None:
    """grok writes the run of text it is streaming as two lines when a background subagent ends meanwhile."""
    parsed = _parse("subagent_ends_mid_reply")
    split_reply = [
        record["params"]["update"]["content"]["text"]
        for offset, record in sorted(parsed.records_by_offset.items())
        if offset in (9564, 10164)
    ]

    replies = [event["text"] for event in parsed.events if event["type"] == "assistant_message" and event["text"]]

    assert replies == [
        "STUB_REPLY k=3: tool finished",
        "STUB_SLOW_REPLY k=7: " + " ".join(f"w{index}" for index in range(1, 28)),
        "STUB_WAKE_REPLY k=9: acknowledged background completion",
    ]
    assert "".join(split_reply) == replies[1]
    reply = next(event for event in parsed.events if event.get("text") == replies[1])
    assert reply["event_id"] == f"grok-{_SUBAGENT_ENDS_MID_REPLY_ROOT}-9564-text"
    assert reply["timestamp"] == record_timestamp(parsed.records_by_offset[9564])


def test_a_split_reply_keeps_the_thinking_of_its_first_line() -> None:
    records = _records(_parse("subagent_ends_mid_reply"))
    thought = _record_of_kind(_parse("streaming"), "agent_thought_chunk")
    first_line_index = _text_line_index(records, "STUB_SLOW_REPLY")

    events = _wire_events("s", [*records[:first_line_index], thought, *records[first_line_index:]], True)

    (reply,) = [event for event in events if event.get("text", "").endswith("w27")]
    assert reply["has_thinking"] is True
    assert reply["event_id"] == f"grok-s-{first_line_index + 1}-text"


def test_text_after_a_record_the_chat_shows_starts_a_message_of_its_own() -> None:
    records = _records(_parse("subagent_ends_mid_reply"))
    second_line_index = _text_line_index(records, " w26 w27")
    stop_note = _record_of_kind(_parse("stop_block"), "hook_annotation")

    events = _wire_events("s", [*records[:second_line_index], stop_note, *records[second_line_index:]], True)

    assert [event["text"] for event in events if event["type"] == "assistant_message" and event["text"]][1:3] == [
        "STUB_SLOW_REPLY k=7: " + " ".join(f"w{index}" for index in range(1, 26)),
        " w26 w27",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain words", "plain words"),
        ("The user sent a message while you were working: <user_query>\nfix it\n</user_query>", "fix it"),
        (
            "The user sent a message while you were working: no wrapper",
            "The user sent a message while you were working: no wrapper",
        ),
    ],
)
def test_unwrap_send_now_returns_the_users_own_words(text: str, expected: str) -> None:
    assert unwrap_send_now(text) == expected


def test_record_timestamp_prefers_the_millisecond_time_and_falls_back_to_seconds() -> None:
    assert record_timestamp({"timestamp": 1792458433, "params": {"_meta": {"agentTimestampMs": 1792458433731}}}) == (
        "2026-10-20T01:07:13.731Z"
    )
    assert record_timestamp({"timestamp": 1792458433, "params": {}}) == "2026-10-20T01:07:13.000Z"
    assert record_timestamp({"params": {}}) == ""


# The records grok 1.0.62 wrote to a top-level session's updates.jsonl for a `/compact` mngr sent, on the stub model.
_COMPACTION_CHECKPOINT_LINE = (
    '{"timestamp":1792752393,"method":"_x.ai/session/update","params":{"sessionId":"9ff7a9cb-81b2-4b74-9f6c-eaa55af03e15",'
    '"update":{"sessionUpdate":"compaction_checkpoint","checkpoint_id":"f7697647-ade2-4622-86e6-dd74b398f556",'
    '"prompt_index_at_compaction":1,"checkpoint_file":"compaction_checkpoints/f7697647-ade2-4622-86e6-dd74b398f556.json",'
    '"schema_version":1,"created_at":"2026-10-23T10:46:33.825292+00:00"},"_meta":{"eventId":'
    '"9ff7a9cb-81b2-4b74-9f6c-eaa55af03e15-14","agentTimestampMs":1792752393825}}}'
)
_AUTO_COMPACT_COMPLETED_LINE = (
    '{"timestamp":1792752393,"method":"_x.ai/session/update","params":{"sessionId":"9ff7a9cb-81b2-4b74-9f6c-eaa55af03e15",'
    '"update":{"sessionUpdate":"auto_compact_completed","tokens_before":6,"tokens_after":6,"summary_preview":null},'
    '"_meta":{"eventId":"9ff7a9cb-81b2-4b74-9f6c-eaa55af03e15-15","agentTimestampMs":1792752393825}}}'
)


def test_a_compaction_of_a_top_level_session_is_the_context_compacted_status() -> None:
    records = [json.loads(_COMPACTION_CHECKPOINT_LINE), json.loads(_AUTO_COMPACT_COMPLETED_LINE)]

    events = _wire_events("root", records, True)

    assert [(event["event_id"], event["timestamp"]) for event in events] == [
        ("grok-root-0-compacted", "2026-10-23T10:46:33.825Z")
    ]
    assert is_context_compacted_event(events[0])
    assert events[0]["role"] == "system"
    assert events[0]["non_turn_tail"] is True


def test_a_subagents_compaction_is_not_the_chats() -> None:
    assert _wire_events("child", [json.loads(_COMPACTION_CHECKPOINT_LINE)], False) == []
