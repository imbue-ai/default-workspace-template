"""Tests for the session JSONL parser."""

import json
from typing import Any

import pytest

from imbue.system_interface.harnesses.claude.session_parser import _SYNTHETIC_MODEL
from imbue.system_interface.harnesses.claude.session_parser import parse_line_detail
from imbue.system_interface.harnesses.claude.session_parser import parse_lines
from imbue.system_interface.harnesses.tool_output import _MAX_PERMISSION_REQUEST_PROBES


def _make_user_line(uuid: str, timestamp: str, content: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {"role": "user", "content": content},
        }
    )


def _make_assistant_line(
    uuid: str,
    timestamp: str,
    text: str,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "claude-opus-4-6",
) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if tool_calls:
        for tc in tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc.get("input", {}),
                }
            )
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        }
    )


def _make_tool_result_line(uuid: str, timestamp: str, tool_use_id: str, output: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": timestamp,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": output, "is_error": False},
                ],
            },
        }
    )


def test_parse_user_message() -> None:
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello")]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Hello"
    assert events[0]["event_id"] == "uuid-1-user"


def test_parse_assistant_message() -> None:
    lines = [_make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "Hi there!")]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "assistant_message"
    assert events[0]["text"] == "Hi there!"
    assert events[0]["model"] == "claude-opus-4-6"
    assert events[0]["tool_calls"] == []


def test_parse_assistant_with_tool_calls() -> None:
    lines = [
        _make_assistant_line(
            "uuid-2",
            "2026-01-01T00:00:01Z",
            "Let me read that.",
            tool_calls=[{"id": "toolu_1", "name": "Read", "input": {"file": "test.txt"}}],
        ),
    ]
    events = parse_lines(lines)
    assert len(events) == 1
    assert len(events[0]["tool_calls"]) == 1
    assert events[0]["tool_calls"][0]["tool_name"] == "Read"
    assert events[0]["tool_calls"][0]["tool_call_id"] == "toolu_1"


def test_parse_tool_result() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_1": "Read"}
    lines = [_make_tool_result_line("uuid-3", "2026-01-01T00:00:02Z", "toolu_1", "file contents")]
    events = parse_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["tool_name"] == "Read"
    assert events[0]["output_chars"] == len("file contents")


def _make_queued_command_line(
    uuid: str,
    timestamp: str,
    prompt: str,
    command_mode: str = "prompt",
) -> str:
    """A message the user typed while the agent was busy.

    Claude Code records this as an ``attachment`` of type ``queued_command``,
    never as a normal ``user`` line. ``commandMode`` is ``prompt`` for verbatim
    user text and ``task-notification`` for background-task completion notices.
    """
    return json.dumps(
        {
            "type": "attachment",
            "uuid": uuid,
            "timestamp": timestamp,
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": command_mode},
        }
    )


def test_parse_queued_command_attachment_emits_user_message() -> None:
    """A message queued while the agent is busy must surface as a user_message.

    Claude Code stores it as a ``queued_command`` attachment rather than a
    ``user`` line (verified against a real Claude 2.1.160 transcript), and the
    agent answers it without ever writing a ``user`` line. If the parser dropped
    it, the message would never appear as a user bubble and the frontend's
    optimistic "Queued" bubble would never reconcile -- staying up even after the
    agent received and answered the message.
    """
    lines = [_make_queued_command_line("uuid-q", "2026-01-01T00:00:00Z", "actually do gmail instead")]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "actually do gmail instead"
    assert events[0]["event_id"] == "uuid-q-queued"


def test_queued_command_reconciles_alongside_normal_turns() -> None:
    """A queued message interleaves correctly with the surrounding conversation."""
    lines = [
        _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "fetch my slack unreads"),
        _make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "Pulling your Slack unreads."),
        _make_queued_command_line("uuid-3", "2026-01-01T00:00:02Z", "actually do gmail instead"),
        _make_assistant_line("uuid-4", "2026-01-01T00:00:03Z", "Switching to Gmail."),
    ]
    events = parse_lines(lines)
    assert [e["type"] for e in events] == [
        "user_message",
        "assistant_message",
        "user_message",
        "assistant_message",
    ]
    assert events[2]["content"] == "actually do gmail instead"


def test_queued_task_notification_attachment_not_emitted() -> None:
    """Background-task notices (commandMode=task-notification) are not user turns."""
    lines = [
        _make_queued_command_line(
            "uuid-n",
            "2026-01-01T00:00:00Z",
            "<task-notification>...</task-notification>",
            command_mode="task-notification",
        )
    ]
    events = parse_lines(lines)
    assert events == []


def test_non_queued_command_attachment_ignored() -> None:
    """Other attachment types (hook output, diagnostics, etc.) produce no events."""
    lines = [
        json.dumps(
            {
                "type": "attachment",
                "uuid": "uuid-h",
                "timestamp": "2026-01-01T00:00:00Z",
                "attachment": {"type": "hook_success", "content": "some hook output"},
            }
        )
    ]
    events = parse_lines(lines)
    assert events == []


def test_blank_queued_command_not_emitted() -> None:
    """A whitespace-only queued prompt is dropped, like a blank user message."""
    lines = [_make_queued_command_line("uuid-b", "2026-01-01T00:00:00Z", "   ")]
    events = parse_lines(lines)
    assert events == []


# Real Claude Code slash-command expansions. The tag ORDER differs between
# custom commands (lead with <command-message>) and built-ins (lead with
# <command-name>), and built-ins indent the trailing tags -- both verified
# against real transcripts. Normalization must handle either.
_CUSTOM_COMMAND_EXPANSION = (
    "<command-message>rebase-merge</command-message>\n"
    "<command-name>/rebase-merge</command-name>\n"
    "<command-args>origin/main</command-args>"
)
_BUILTIN_COMMAND_EXPANSION = (
    "<command-name>/clear</command-name>\n"
    "            <command-message>clear</command-message>\n"
    "            <command-args></command-args>"
)


def test_slash_command_expansion_normalized_to_typed_text() -> None:
    """A slash command renders as the '/name args' the user actually typed.

    Claude Code does not store a slash command verbatim; it expands it into an
    XML-ish <command-name>/<command-args> block. The parser rebuilds the typed
    text so (a) the user bubble shows '/rebase-merge origin/main' rather than the
    raw expansion and (b) it matches what the frontend's optimistic bubble stored,
    so reconciliation (whitespace-normalized content match) succeeds.
    """
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", _CUSTOM_COMMAND_EXPANSION)]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "/rebase-merge origin/main"


def test_slash_command_expansion_with_empty_args_drops_trailing_space() -> None:
    """A no-argument command (built-in tag order, indented, empty args) yields
    just '/clear' -- the rebuilt text carries no dangling whitespace around the
    (absent) args."""
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", _BUILTIN_COMMAND_EXPANSION)]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["content"] == "/clear"



def test_queued_slash_command_expansion_normalized() -> None:
    """A slash command queued while the agent is busy is normalized the same way
    on the queued_command path, so it too reconciles against its optimistic
    bubble."""
    lines = [_make_queued_command_line("uuid-q", "2026-01-01T00:00:00Z", _CUSTOM_COMMAND_EXPANSION)]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["content"] == "/rebase-merge origin/main"


def test_non_command_text_with_angle_brackets_untouched() -> None:
    """Ordinary user text that happens to contain angle brackets but no
    <command-name> tag passes through unchanged."""
    text = "does <Foo> compile when T <: Bar?"
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", text)]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["content"] == text


def test_parse_conversation_sequence() -> None:
    lines = [
        _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello"),
        _make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "Hi!"),
        _make_user_line("uuid-3", "2026-01-01T00:00:02Z", "How are you?"),
        _make_assistant_line("uuid-4", "2026-01-01T00:00:03Z", "Good!"),
    ]
    events = parse_lines(lines)
    assert len(events) == 4
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "assistant_message"
    assert events[2]["type"] == "user_message"
    assert events[3]["type"] == "assistant_message"


def test_deduplication() -> None:
    lines = [_make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello")]
    existing_ids = {"uuid-1-user"}
    events = parse_lines(lines, existing_event_ids=existing_ids)
    assert len(events) == 0


def test_skips_non_conversation_events() -> None:
    lines = [
        json.dumps({"type": "progress", "uuid": "uuid-p", "timestamp": "2026-01-01T00:00:00Z"}),
        json.dumps({"type": "file-history-snapshot", "uuid": "uuid-f", "timestamp": "2026-01-01T00:00:00Z"}),
        _make_user_line("uuid-1", "2026-01-01T00:00:01Z", "Hello"),
    ]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"


def test_skips_blank_and_invalid_lines() -> None:
    lines = ["", "  ", "not json", _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Hello")]
    events = parse_lines(lines)
    assert len(events) == 1


def test_tool_result_only_user_message_not_emitted_as_user_message() -> None:
    """A user message containing only tool results should not produce a user_message event."""
    lines = [_make_tool_result_line("uuid-3", "2026-01-01T00:00:02Z", "toolu_1", "result")]
    events = parse_lines(lines)
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"


def test_interrupt_sentinel_user_message_not_emitted() -> None:
    """The ``[Request interrupted by user]`` sentinel must not surface as a user_message.

    Claude writes this control text to the user channel when the user interrupts
    a turn. Treating it as a real prompt would leave the activity indicator
    pinned on "Thinking..." after every interrupt, since the indicator's tail-
    event heuristic equates "tail = user_message" with "Claude is about to
    reply." Verify both string content and array content forms.
    """
    string_form = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "[Request interrupted by user]"},
        }
    )
    array_form = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user]"}],
            },
        }
    )
    events = parse_lines([string_form, array_form])
    assert events == []


def test_mid_tool_interrupt_sentinel_user_message_not_emitted() -> None:
    """The mid-tool ``[Request interrupted by user for tool use]`` variant is also suppressed.

    Claude writes this shape when the interrupt lands while a tool is running (the dominant stop
    scenario). Like the plain sentinel it is a control marker, not real user input, so it must not
    surface as a user_message (which would leave a phantom user bubble and pin the tail
    heuristic). Both the string and array content forms are covered.
    """
    string_form = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "[Request interrupted by user for tool use]"},
        }
    )
    array_form = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}],
            },
        }
    )
    events = parse_lines([string_form, array_form])
    assert events == []


def test_ordinary_user_text_resembling_the_sentinel_still_passes() -> None:
    """Only the interrupt-sentinel opening is suppressed; ordinary user text still surfaces.

    A prompt that merely mentions the phrase mid-sentence (not as the sentinel's leading form)
    is a real user turn and must render.
    """
    line = _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Please explain what [Request interrupted by user] means")
    events = parse_lines([line])
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "Please explain what [Request interrupted by user] means"


def test_events_sorted_by_timestamp() -> None:
    lines = [
        _make_assistant_line("uuid-2", "2026-01-01T00:00:02Z", "Second"),
        _make_user_line("uuid-1", "2026-01-01T00:00:01Z", "First"),
    ]
    events = parse_lines(lines)
    assert len(events) == 2
    assert events[0]["type"] == "user_message"
    assert events[1]["type"] == "assistant_message"


def test_tool_input_stays_off_the_event() -> None:
    long_input = {"data": "x" * 300}
    lines = [
        _make_assistant_line(
            "uuid-1",
            "2026-01-01T00:00:00Z",
            "test",
            tool_calls=[{"id": "toolu_1", "name": "Read", "input": long_input}],
        ),
    ]
    events = parse_lines(lines)
    tool_call = events[0]["tool_calls"][0]
    # Payload-free wire: no raw input on the event, just its size; the full input comes
    # back through parse_line_detail on expand.
    assert "input_preview" not in tool_call
    assert tool_call["input_chars"] > 300


def test_tool_output_stays_off_the_event() -> None:
    long_output = "x" * 3000
    tool_name_by_call_id: dict[str, str] = {"toolu_1": "Bash"}
    lines = [_make_tool_result_line("uuid-1", "2026-01-01T00:00:00Z", "toolu_1", long_output)]
    events = parse_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert "output" not in events[0]
    assert events[0]["output_chars"] == 3000


def test_detail_reconstructs_full_input_and_output() -> None:
    """parse_line_detail hands back the whole payloads the resident events omit --
    untruncated, however large."""
    long_input = {"data": "x" * 300}
    assistant_line = _make_assistant_line(
        "uuid-in",
        "2026-01-01T00:00:00Z",
        "test",
        tool_calls=[{"id": "toolu_1", "name": "Read", "input": long_input}],
    )
    detail = parse_line_detail(assistant_line)["uuid-in-assistant"]
    assert "x" * 300 in detail["inputs_by_tool_call_id"]["toolu_1"]
    # Claude's thinking is encrypted and useless; it is never surfaced.
    assert detail["thinking"] is None

    result_line = _make_tool_result_line("uuid-out", "2026-01-01T00:00:00Z", "toolu_1", "y" * 3000)
    detail = parse_line_detail(result_line)["uuid-out-tool_result-toolu_1"]
    assert detail["output"] == "y" * 3000


def test_agent_tool_use_exposes_description_and_subagent_type() -> None:
    lines = [
        _make_assistant_line(
            "uuid-1",
            "2026-01-01T00:00:00Z",
            "spawning",
            tool_calls=[
                {
                    "id": "toolu_agent",
                    "name": "Agent",
                    "input": {"description": "explore foo", "subagent_type": "Explore", "prompt": "do it"},
                }
            ],
        ),
    ]
    events = parse_lines(lines)
    tc = events[0]["tool_calls"][0]
    assert tc["description"] == "explore foo"
    assert tc["subagent_type"] == "Explore"


def test_non_agent_tool_use_has_no_description_or_subagent_type() -> None:
    lines = [
        _make_assistant_line(
            "uuid-1",
            "2026-01-01T00:00:00Z",
            "reading",
            tool_calls=[{"id": "toolu_read", "name": "Read", "input": {"file_path": "/x", "description": "nope"}}],
        ),
    ]
    events = parse_lines(lines)
    tc = events[0]["tool_calls"][0]
    assert "description" not in tc
    assert "subagent_type" not in tc


def _make_agent_tool_result_line(
    uuid: str,
    timestamp: str,
    tool_use_id: str,
    output: str,
    structured_agent_id: str | None = None,
) -> str:
    raw: dict[str, Any] = json.loads(_make_tool_result_line(uuid, timestamp, tool_use_id, output))
    if structured_agent_id is not None:
        raw["toolUseResult"] = {"status": "completed", "agentId": structured_agent_id}
    return json.dumps(raw)


def test_agent_tool_result_uses_structured_agent_id() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Exploration complete.",
            structured_agent_id="abc123",
        ),
    ]
    events = parse_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert events[0]["type"] == "tool_result"
    assert events[0]["subagent_id"] == "abc123"


def test_agent_tool_result_falls_back_to_text_trailer() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Exploration complete.\nagentId: legacy999",
            structured_agent_id=None,
        ),
    ]
    events = parse_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert events[0]["subagent_id"] == "legacy999"


def test_agent_tool_result_without_any_agent_id_omits_field() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Exploration complete with no link info.",
            structured_agent_id=None,
        ),
    ]
    events = parse_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert len(events) == 1
    assert "subagent_id" not in events[0]


def test_agent_tool_result_prefers_structured_over_trailer() -> None:
    tool_name_by_call_id: dict[str, str] = {"toolu_agent": "Agent"}
    lines = [
        _make_agent_tool_result_line(
            "uuid-a",
            "2026-01-01T00:00:00Z",
            "toolu_agent",
            "Done.\nagentId: trailerWins",
            structured_agent_id="structuredWins",
        ),
    ]
    events = parse_lines(lines, tool_name_by_call_id=tool_name_by_call_id)
    assert events[0]["subagent_id"] == "structuredWins"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Here is the file contents.", False, id="plain-assistant-text"),
        pytest.param("", False, id="empty-text"),
        pytest.param(
            "Not logged in \u00b7 Please run /login to authenticate.",
            True,
            id="not-logged-in",
        ),
        pytest.param(
            "I received an error: Invalid API key. Please update your credentials.",
            True,
            id="invalid-api-key",
        ),
        pytest.param(
            "OAuth token has been revoked; re-authentication required.",
            True,
            id="oauth-revoked",
        ),
        pytest.param("Error: OAuth token has expired.", True, id="oauth-expired"),
        pytest.param(
            "OAuth token does not meet scope requirements for this operation.",
            True,
            id="oauth-scope",
        ),
        pytest.param(
            'API returned: {"type": "authentication_error", "message": "..."}',
            True,
            id="authentication-error-type",
        ),
        pytest.param("API Error: 401 Unauthorized", True, id="api-401"),
        pytest.param("Invalid authentication credentials provided.", True, id="invalid-credentials"),
        pytest.param(
            "Your credit balance is too low to make this request.",
            True,
            id="credit-balance-too-low",
        ),
        pytest.param("This organization has been disabled.", True, id="org-disabled"),
    ],
)
def test_assistant_message_auth_error_flag(text: str, expected: bool) -> None:
    """The flag is read off Claude Code's own framework notices, which carry `<synthetic>`."""
    lines = [_make_assistant_line("uuid-1", "2026-01-01T00:00:00Z", text, model=_SYNTHETIC_MODEL)]
    events = parse_lines(lines)
    assert events[0]["is_auth_error"] is expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "When your customer has an **invalid api key** you should help them rotate it.",
            id="agent-explaining-invalid-api-key",
        ),
        pytest.param(
            'Handle the 401 case: {"type": "authentication_error"} means the key is wrong.',
            id="agent-quoting-an-error-body",
        ),
        pytest.param("Check whether the OAuth token has expired before retrying.", id="agent-prose"),
    ],
)
def test_a_real_reply_that_merely_talks_about_auth_is_not_an_auth_error(text: str) -> None:
    """An agent helping with a credential says these things in ordinary prose.

    Ungated, its own reply was painted as a failed turn with a "Sign in again" button under it
    -- the more likely reading of the words, since a coding agent discusses auth far more often
    than it fails it. The API-error check was already gated this way; this one was not.
    """
    lines = [_make_assistant_line("uuid-1", "2026-01-01T00:00:00Z", text)]
    events = parse_lines(lines)
    assert events[0]["is_auth_error"] is False
    assert events[0]["is_api_error"] is False


def test_user_message_with_array_content() -> None:
    line = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Part one"},
                    {"type": "text", "text": "Part two"},
                ],
            },
        }
    )
    events = parse_lines([line])
    assert len(events) == 1
    assert events[0]["content"] == "Part one\nPart two"


def test_resume_continuation_user_message_emitted_hidden() -> None:
    """Claude Code's "Continue from where you left off." resume marker (an ``isMeta``
    framework injection) is emitted with the render decision already made: hidden, and
    non-turn-tail. The raw flag never crosses the wire.
    """
    line = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-r",
            "timestamp": "2026-01-01T00:00:00Z",
            "isMeta": True,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Continue from where you left off."}],
            },
        }
    )
    events = parse_lines([line])
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["display"] == "hidden"
    assert events[0]["non_turn_tail"] is True
    assert "is_meta" not in events[0]


def test_image_metadata_note_emitted_hidden() -> None:
    """Claude Code's isMeta image coordinate note arrives with ``display: hidden`` so it
    never leaks through as a bare user bubble.
    """
    line = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-img",
            "timestamp": "2026-01-01T00:00:00Z",
            "isMeta": True,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "[Image: original 1800x2800, displayed at 1286x2000. Multiply coordinates by 1.40 to map to original image.]",
                    }
                ],
            },
        }
    )
    events = parse_lines([line])
    assert len(events) == 1
    assert events[0]["display"] == "hidden"


def test_genuine_user_message_has_no_display_decision() -> None:
    """A real human turn carries no ``display`` key, so the frontend shows the baseline bubble."""
    events = parse_lines([_make_user_line("uuid-h", "2026-01-01T00:00:00Z", "hello there")])
    assert len(events) == 1
    assert "is_meta" not in events[0]


def test_compaction_summary_user_message_emitted_as_status() -> None:
    """Claude Code's post-auto-compaction record (``isCompactSummary``) is
    emitted as a subtle status message ("Context was compacted") with display=status.
    """
    line = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-c",
            "timestamp": "2026-01-01T00:00:00Z",
            "isCompactSummary": True,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "This session is being continued from a previous conversation that ran out of context.",
                    }
                ],
            },
        }
    )
    events = parse_lines([line])
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["display"] == "status"
    assert events[0]["content"] == "Context was compacted"
    assert (
        events[0]["display_body"]
        == "This session is being continued from a previous conversation that ran out of context."
    )
    assert events[0]["role"] == "system"
    assert events[0]["non_turn_tail"] is True
    assert "is_meta" not in events[0] and "is_compact_summary" not in events[0]


def test_compaction_command_and_output_dropped() -> None:
    """The /compact command and <local-command-stdout> compaction output are dropped."""
    cmd_line = _make_user_line(
        "uuid-cmd",
        "2026-01-01T00:00:00Z",
        "<command-name>/compact</command-name>\n<command-message>compact</command-message>",
    )
    plain_cmd_line = _make_user_line("uuid-cmd2", "2026-01-01T00:00:01Z", "/compact")
    out_line = _make_user_line(
        "uuid-out",
        "2026-01-01T00:00:02Z",
        "<local-command-stdout>\x1b[2mCompacted (ctrl+o to see full summary)\x1b[22m</local-command-stdout>",
    )
    events = parse_lines([cmd_line, plain_cmd_line, out_line])
    assert len(events) == 0



def test_synthetic_model_assistant_message_not_emitted() -> None:
    """The synthetic "No response requested." reply -- the answer half of the
    resume turn-pair -- is bookkeeping, not a real agent turn, and must not
    surface as an assistant_message event.
    """
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "uuid-s",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "No response requested."}],
                "stop_reason": "stop_sequence",
                "usage": {},
            },
        }
    )
    assert parse_lines([line]) == []


def test_resume_marker_filter_is_gated_and_does_not_over_hide() -> None:
    """The resume filters are precise: a human who actually types the
    continuation words (a non-meta message) is still shown, and a real-model
    assistant that happens to say "No response requested." is still shown.
    """
    typed = _make_user_line("uuid-1", "2026-01-01T00:00:00Z", "Continue from where you left off.")
    real_reply = _make_assistant_line("uuid-2", "2026-01-01T00:00:01Z", "No response requested.")
    events = parse_lines([typed, real_reply])
    assert [e["type"] for e in events] == ["user_message", "assistant_message"]
    assert events[0]["content"] == "Continue from where you left off."
    assert events[1]["text"] == "No response requested."


def test_synthetic_api_error_message_is_still_shown() -> None:
    """Claude Code stamps the synthetic model on API-error and auth notices
    too (e.g. "API Error: 529 Overloaded", "Please run /login"). Those tell the
    user their turn failed and must stay visible -- only the exact
    "No response requested." resume reply is hidden, not every synthetic message.
    """
    error_text = "API Error: 529 Overloaded. This is a server-side issue, usually temporary."
    line = json.dumps(
        {
            "type": "assistant",
            "uuid": "uuid-e",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": error_text}],
                "stop_reason": "stop_sequence",
                "usage": {},
            },
        }
    )
    events = parse_lines([line])
    assert [e["type"] for e in events] == ["assistant_message"]
    assert events[0]["text"] == error_text


def test_api_error_flagged_only_on_synthetic_messages() -> None:
    """A synthetic API-error notice is flagged (so the frontend styles it as an error);
    a REAL assistant message that merely quotes 'API Error: 500' is NOT flagged -- a
    coding chat discussing errors must not be painted as a provider outage."""
    synthetic = json.dumps(
        {
            "type": "assistant",
            "uuid": "uuid-s",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "API Error: 529 Overloaded"}],
                "stop_reason": "stop_sequence",
                "usage": {},
            },
        }
    )
    real = _make_assistant_line(
        "uuid-r", "2026-01-01T00:00:03Z", "The server sometimes returns `API Error: 500` on retry."
    )
    synthetic_event = parse_lines([synthetic])[0]
    assert synthetic_event["is_api_error"] is True
    assert synthetic_event["is_provider_fault"] is True

    real_event = parse_lines([real])[0]
    assert real_event["is_api_error"] is False
    assert real_event["is_provider_fault"] is False


def test_tk_transition_is_stamped_resident() -> None:
    """A tk transition line (`Updated <id> -> <status>`) is stamped resident however deep
    in the output it sits, so the progress view never loses a step transition when a tk
    command is batched after verbose output (the raw output never reaches the wire)."""
    output = ("x" * 5000) + "\nUpdated cod-step-s1 -> closed\n"
    lines = [_make_tool_result_line("uuid-trunc", "2026-01-01T00:00:02Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert events[0]["type"] == "tool_result"
    assert "Updated cod-step-s1 -> closed" in events[0]["tk_stamp"]
    assert "output" not in events[0]


def test_tk_step_decoration_is_stamped_resident() -> None:
    """The `tk-step <id> title|summary: ...` decoration lines that a step's start/close
    emit are stamped resident, so the progress view can read titles and summaries straight
    off the event."""
    output = (
        ("x" * 5000)
        + "\nUpdated cod-step-abcd -> closed\n"
        + "tk-step cod-step-abcd title: Register the new theme\n"
        + "tk-step cod-step-abcd summary: Wired the theme into the toggle.\n"
    )
    lines = [_make_tool_result_line("uuid-dec", "2026-01-01T00:00:02Z", "toolu_1", output)]
    events = parse_lines(lines)
    stamped = events[0]["tk_stamp"]
    assert "Updated cod-step-abcd -> closed" in stamped
    assert "tk-step cod-step-abcd title: Register the new theme" in stamped
    assert "tk-step cod-step-abcd summary: Wired the theme into the toggle." in stamped
    assert len(stamped) < len(output)


def test_tk_lifecycle_command_is_stamped_resident() -> None:
    """tk create/start/close commands are stamped resident whole so the historical input
    fallback can recover titles and summaries without fetching the input. Batched
    `S1=$(tk create ...)` forms and long `tk close <id> "<summary>"` calls both qualify;
    a non-tk command carries no stamp."""
    batched_create = "\n".join(
        f'S{i}=$(tk create --step "Step number {i} with a fairly long descriptive title here")' for i in range(1, 6)
    )
    long_close = 'tk close cod-step-abcd "' + ("a very detailed summary of the work " * 6).strip() + '"'
    long_non_tk = "echo " + ("y" * 400)
    lines = [
        _make_assistant_line(
            "uuid-tk",
            "2026-01-01T00:00:00Z",
            "working",
            tool_calls=[
                {"id": "toolu_create", "name": "Bash", "input": {"command": batched_create}},
                {"id": "toolu_close", "name": "Bash", "input": {"command": long_close}},
                {"id": "toolu_echo", "name": "Bash", "input": {"command": long_non_tk}},
            ],
        ),
    ]
    events = parse_lines(lines)
    calls = {tc["tool_call_id"]: tc for tc in events[0]["tool_calls"]}
    assert calls["toolu_create"]["tk_command"] == batched_create
    assert calls["toolu_close"]["tk_command"] == long_close
    assert "tk_command" not in calls["toolu_echo"]


def test_tk_mentioned_in_quoted_arg_is_not_stamped() -> None:
    """A command that merely *mentions* `tk close ...` inside a quoted argument is NOT a tk
    lifecycle call, so it carries no tk_command stamp. The shared shlex parser
    distinguishes this from a real tk invocation."""
    mentions_tk = 'echo "remember to tk close cod-step-x once ' + ("the work is fully done " * 12).strip() + '"'
    lines = [
        _make_assistant_line(
            "uuid-mention",
            "2026-01-01T00:00:00Z",
            "working",
            tool_calls=[{"id": "toolu_mention", "name": "Bash", "input": {"command": mentions_tk}}],
        ),
    ]
    events = parse_lines(lines)
    assert "tk_command" not in events[0]["tool_calls"][0]


@pytest.mark.parametrize("line_type", ["assistant", "user"])
def test_null_message_line_is_dropped_not_crashed(line_type: str) -> None:
    """A line with a present-but-null ``message`` must be dropped, not raised on.

    ``raw.get("message", {})`` returns ``None`` for a present-but-null key (the
    default only applies to a *missing* key), and Claude Code does write lines
    with ``"message": null``. Without the guard the parser raises AttributeError,
    which kills the watcher thread and wedges the read path (the byte offset never
    advances, so every poll re-reads and re-crashes the same line forever).
    """
    line = json.dumps({"type": line_type, "uuid": "u-null", "timestamp": "2026-01-01T00:00:00Z", "message": None})
    assert parse_lines([line]) == []


def test_non_dict_message_is_dropped_not_crashed() -> None:
    """A ``message`` that is present but not an object (e.g. a bare string) is
    likewise dropped rather than crashing the per-line parse."""
    line = json.dumps(
        {"type": "assistant", "uuid": "u-str", "timestamp": "2026-01-01T00:00:00Z", "message": "not an object"}
    )
    assert parse_lines([line]) == []


def test_null_message_line_does_not_wedge_following_valid_lines() -> None:
    """A null-message line in the middle of a batch is skipped without aborting
    the run -- the valid user message after it is still parsed."""
    lines = [
        json.dumps({"type": "assistant", "uuid": "u-bad", "timestamp": "2026-01-01T00:00:00Z", "message": None}),
        _make_user_line("u-good", "2026-01-01T00:00:01Z", "still here"),
    ]
    events = parse_lines(lines)
    assert [e["content"] for e in events if e["type"] == "user_message"] == ["still here"]


def _make_permission_request_output(rationale: str) -> str:
    """The stdout of a latchkey permission-request POST: curl's progress meter,
    then the created request pretty-printed the way the gateway writes it --
    including the `target` and `effect` fields it echoes but the card ignores."""
    body = json.dumps(
        {
            "request_id": "885711ec07bf47239d71294e1534330b",
            "agent_id": "agent-28dc23edadd34caeaba58441ac8e7218",
            "rationale": rationale,
            "request_type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all", "slack-write-all"]},
            "target": "/home/user/.latchkey/permissions.json",
            "effect": {"rules": [{"slack-api::me@example.com": ["slack-read-all", "slack-write-all"]}]},
        },
        indent=2,
    )
    meter = "  % Total    % Received % Xferd  Average Speed   Time    Time     Time  Current\n" * 3
    return meter + body + "\n"


_LONG_RATIONALE = "I need to read the eng-releases channel to summarize the deploy thread. " * 30
_REQUEST_ID = "885711ec07bf47239d71294e1534330b"


def test_permission_request_rides_the_event_however_long_the_output() -> None:
    """The parsed request rides on the event whole, however large the response -- the raw
    output never reaches the wire, so the structured field is the card's only source."""
    output = _make_permission_request_output(_LONG_RATIONALE)
    assert len(output) > 2000
    lines = [_make_tool_result_line("uuid-perm", "2026-01-01T00:00:00Z", "toolu_1", output)]
    events = parse_lines(lines)
    request = events[0]["permission_request"]
    assert request["request_id"] == _REQUEST_ID
    assert request["rationale"] == _LONG_RATIONALE
    assert request["payload"]["scope"] == "slack-api"
    assert "output" not in events[0]


def test_permission_request_parsed_when_not_last_on_stdout() -> None:
    """The object's end comes from the JSON decoder, not from an assumption that it runs to
    the end of stdout, so a command that printed more after the response still yields a
    complete request."""
    output = _make_permission_request_output(_LONG_RATIONALE) + "\nrequest submitted, waiting for the user\n"
    lines = [_make_tool_result_line("uuid-tail", "2026-01-01T00:00:01Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert events[0]["permission_request"]["rationale"] == _LONG_RATIONALE


def test_permission_request_found_past_earlier_json_in_output() -> None:
    """Candidate `{`s are walked rather than the first one trusted, so a batched command
    that printed other JSON before the POST still yields the request."""
    output = '{"rules": []}\n' + _make_permission_request_output(_LONG_RATIONALE)
    lines = [_make_tool_result_line("uuid-pre", "2026-01-01T00:00:02Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert events[0]["permission_request"]["rationale"] == _LONG_RATIONALE


def test_short_permission_request_output_is_attached_too() -> None:
    """A small response attaches the parsed request just the same, so the card reads one
    field regardless of the response's size."""
    output = (
        '{"request_id":"fs-1","rationale":"write the report","request_type":"file-sharing",'
        '"payload":{"path":"/tmp/report","access":"WRITE"}}'
    )
    lines = [_make_tool_result_line("uuid-short", "2026-01-01T00:00:03Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert events[0]["permission_request"]["payload"]["access"] == "WRITE"


def test_tk_decoration_stamps_alongside_a_permission_request() -> None:
    """When both land in one output, both structured facts ride the event: the tk lines in
    the stamp and the request in its own field."""
    output = "Updated cod-step-s1 -> closed\n" + _make_permission_request_output(_LONG_RATIONALE)
    lines = [_make_tool_result_line("uuid-both", "2026-01-01T00:00:04Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert "Updated cod-step-s1 -> closed" in events[0]["tk_stamp"]
    assert events[0]["permission_request"]["request_id"] == _REQUEST_ID


def test_ordinary_large_output_carries_no_request_or_stamp() -> None:
    """A long output that merely contains JSON carries no permission-request field and no
    tk stamp -- just its size."""
    output = json.dumps({"items": [{"id": index, "name": "x" * 50} for index in range(100)]})
    assert len(output) > 2000
    lines = [_make_tool_result_line("uuid-big", "2026-01-01T00:00:05Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert "permission_request" not in events[0]
    assert "tk_stamp" not in events[0]
    assert events[0]["output_chars"] == len(output)


def test_error_result_stamps_a_resident_snippet() -> None:
    """A failed call stays glanceable without a fetch: its first line rides the event."""
    output = "FileNotFoundError: no such file /x/y.py\n" + ("trace " * 800)
    line = json.dumps(
        {
            "type": "user",
            "uuid": "uuid-err",
            "timestamp": "2026-01-01T00:00:06Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": output, "is_error": True}],
            },
        }
    )
    events = parse_lines([line])
    assert events[0]["is_error"] is True
    assert events[0]["error_snippet"] == "FileNotFoundError: no such file /x/y.py"
    assert "output" not in events[0]


def test_unrelated_request_id_json_is_not_a_permission_request() -> None:
    """Recognition is narrow: an API response carrying a `request_id` but none of
    a permission request's shape is left alone, so ordinary tool output neither
    grows a permission-request field nor dodges the output limit."""
    output = json.dumps({"request_id": "abc123", "status": "ok", "data": "y" * 3000})
    lines = [_make_tool_result_line("uuid-other", "2026-01-01T00:00:06Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert "permission_request" not in events[0]
    assert "output" not in events[0]


def test_oversized_permission_request_falls_back_to_truncation() -> None:
    """Preservation is bounded: a body past the preserved-object ceiling is
    head-truncated like any other output and is not carried on the event, so a
    pathological response cannot widen the output limit without bound."""
    output = _make_permission_request_output("y" * 9000)
    lines = [_make_tool_result_line("uuid-huge", "2026-01-01T00:00:07Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert "permission_request" not in events[0]
    assert "output" not in events[0]


def test_permission_request_probe_loop_is_capped() -> None:
    """Candidate probing is bounded: each failed probe costs work proportional
    to the output length (JSONDecodeError's line/column bookkeeping rescans the
    document), so a wall of braces ahead of the marker is O(braces x length) --
    measured at ~3 s for 100KB of braces before the cap existed. Past the probe
    cap the result is treated as ordinary output -- head-truncated, no request
    attached -- even though a well-formed request sits beyond the wall."""
    output = "{" * (_MAX_PERMISSION_REQUEST_PROBES * 3) + _make_permission_request_output("hidden beyond the cap")
    lines = [_make_tool_result_line("uuid-wall", "2026-01-01T00:00:08Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert "permission_request" not in events[0]
    assert "output" not in events[0]


def test_permission_request_within_probe_cap_still_parses() -> None:
    """The cap is headroom, not a hair trigger: a request behind far more
    braces than any legitimate output carries -- but still within the cap --
    is found and preserved just the same."""
    output = "{" * (_MAX_PERMISSION_REQUEST_PROBES - 10) + _make_permission_request_output(_LONG_RATIONALE)
    lines = [_make_tool_result_line("uuid-under", "2026-01-01T00:00:09Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert events[0]["permission_request"]["request_id"] == _REQUEST_ID
    assert events[0]["permission_request"]["rationale"] == _LONG_RATIONALE


def test_deeply_nested_json_probe_does_not_crash_parsing() -> None:
    """Probing absurdly deep nesting makes the C scanner raise RecursionError,
    which is not a JSONDecodeError and previously escaped the probe loop and
    crashed the whole parse. It is now swallowed and the result treated as
    ordinary output."""
    output = '{"a": ' * 100_000 + '"request_id"'
    lines = [_make_tool_result_line("uuid-deep", "2026-01-01T00:00:10Z", "toolu_1", output)]
    events = parse_lines(lines)
    assert "permission_request" not in events[0]
    assert "output" not in events[0]


def test_queued_command_attachment_carries_the_render_decision() -> None:
    """The queued-path emitter stamps display/non_turn_tail exactly like the normal path.

    A /model parked while claude is mid-turn must stay hidden (and must not pin the dot on
    Thinking) -- pre-decision wiring, the frontend's content sniffing covered this path; now
    nothing downstream re-derives it, so the emitter itself must decide.
    """
    events = parse_lines([_make_queued_command_line("uuid-q1", "2026-01-01T00:00:00Z", "/model sonnet")])
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["display"] == "hidden"
    assert events[0]["non_turn_tail"] is True


def test_chip_classification_survives_an_attachment_block() -> None:
    """Whole-string-anchored detectors run on the attachment-stripped text, and a chip's
    display body shows the message text rather than the raw attachment markdown."""
    content = (
        "<agentic-browser-fleet>Browser foo-1 is free</agentic-browser-fleet>"
        "\n\nSee attachment here: ![](/uploads/x.png)"
    )
    events = parse_lines([_make_user_line("uuid-att", "2026-01-01T00:00:00Z", content)])
    assert len(events) == 1
    assert events[0]["display"] == "chip"
    assert events[0]["display_label"] == "Browser fleet"
    assert events[0]["display_body"] == "Browser foo-1 is free"
