from pathlib import Path
from typing import Any

from imbue.system_interface.handoff_context import MAX_QUOTED_CHARS
from imbue.system_interface.handoff_context import MAX_RENDERED_EVENTS
from imbue.system_interface.handoff_context import build_handoff_context
from imbue.system_interface.handoff_context import build_handoff_first_message
from imbue.system_interface.handoff_context import handoff_dir_for_workspace
from imbue.system_interface.handoff_context import render_conversation
from imbue.system_interface.handoff_context import write_handoff_context
from imbue.system_interface.harnesses.harness_type import HarnessType


def _user(content: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user_message", "event_id": f"u-{content[:8]}", "content": content, **extra}


def _assistant(text: str, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "type": "assistant_message",
        "event_id": f"a-{text[:8]}",
        "text": text,
        "tool_calls": tool_calls or [],
    }


def _tool_result(tool_name: str, is_error: bool, error_snippet: str = "") -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "tool_result",
        "event_id": f"r-{tool_name}-{is_error}",
        "tool_name": tool_name,
        "is_error": is_error,
        "output_chars": 100,
    }
    if error_snippet:
        event["error_snippet"] = error_snippet
    return event


def test_a_conversation_renders_its_turns_in_order() -> None:
    rendered = render_conversation([_user("Fix the login bug"), _assistant("Looking at it now"), _user("Thanks")])

    assert rendered.index("Fix the login bug") < rendered.index("Looking at it now")
    assert rendered.index("Looking at it now") < rendered.index("Thanks")
    assert rendered.count("### User") == 2
    assert rendered.count("### Assistant") == 1


def test_tool_calls_are_named_but_their_inputs_are_not_inlined() -> None:
    rendered = render_conversation(
        [
            _assistant(
                "Reading the file",
                [{"tool_name": "Read", "header_label": "auth.py", "caption_label": "", "input_chars": 900}],
            )
        ]
    )

    assert "Read: auth.py" in rendered
    # The point of the handover is the shape of the conversation; a file read is
    # something the new agent can simply do again.
    assert "input_chars" not in rendered


def test_only_failing_tool_results_are_rendered() -> None:
    rendered = render_conversation(
        [
            _tool_result("Bash", is_error=False),
            _tool_result("Bash", is_error=True, error_snippet="command not found: pnpm"),
        ]
    )

    assert rendered.count("Tool failure") == 1
    assert "command not found: pnpm" in rendered


def test_hidden_user_messages_are_left_out() -> None:
    rendered = render_conversation([_user("/model opus", display="hidden"), _user("real question")])

    # Hidden messages have no DOM in the chat either, so handing them over as the user's
    # words would misrepresent the conversation.
    assert "/model opus" not in rendered
    assert "real question" in rendered


def test_empty_and_unknown_events_are_skipped() -> None:
    rendered = render_conversation(
        [
            {"type": "special", "event_id": "s1", "kind": "turn_started"},
            _user("   "),
            _assistant(""),
            {"type": "something_new", "event_id": "x1"},
            _user("the only real turn"),
        ]
    )

    assert rendered == "### User\n\nthe only real turn"


def test_long_prose_is_elided_rather_than_dropped() -> None:
    rendered = render_conversation([_user("x" * (MAX_QUOTED_CHARS + 500))])

    assert "more characters" in rendered
    assert len(rendered) < MAX_QUOTED_CHARS + 500


def test_a_very_long_conversation_keeps_its_most_recent_turns() -> None:
    events = [_user(f"turn {index}") for index in range(MAX_RENDERED_EVENTS + 50)]

    context = build_handoff_context(events, HarnessType.CLAUDE, HarnessType.CODEX)

    assert f"turn {MAX_RENDERED_EVENTS + 49}" in context
    assert "turn 0" not in context
    assert f"most recent {MAX_RENDERED_EVENTS}" in context


def test_the_context_header_names_both_harnesses_and_frames_the_history_as_past() -> None:
    context = build_handoff_context([_user("hello")], HarnessType.CLAUDE, HarnessType.CODEX)

    assert "claude" in context
    assert "codex" in context
    # Without this framing the successor's instinct is to re-answer the last question in
    # the transcript instead of continuing past it.
    assert "already happened" in context


def test_writing_the_context_file_returns_a_readable_path(tmp_path: Path) -> None:
    path = write_handoff_context(tmp_path, "op-1", [_user("Fix the login bug")], HarnessType.CLAUDE, HarnessType.CODEX)

    assert path is not None
    assert path == handoff_dir_for_workspace(tmp_path, "op-1") / "context.md"
    assert "Fix the login bug" in path.read_text(encoding="utf-8")


def test_two_switches_of_one_chat_do_not_share_a_context_file(tmp_path: Path) -> None:
    first = write_handoff_context(tmp_path, "op-1", [_user("first")], HarnessType.CLAUDE, HarnessType.CODEX)
    second = write_handoff_context(tmp_path, "op-2", [_user("second")], HarnessType.CODEX, HarnessType.CLAUDE)

    assert first is not None and second is not None
    assert first != second
    assert "first" in first.read_text(encoding="utf-8")
    assert "second" in second.read_text(encoding="utf-8")


def test_an_unwritable_state_dir_does_not_raise(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    # Losing the history is a worse handover; refusing the switch would strand the user
    # on the harness they asked to leave.
    assert write_handoff_context(blocked, "op-1", [_user("hi")], HarnessType.CLAUDE, HarnessType.CODEX) is None


def test_the_first_message_points_at_the_file_instead_of_carrying_the_history(tmp_path: Path) -> None:
    path = tmp_path / "context.md"

    message = build_handoff_first_message(path, HarnessType.CLAUDE, HarnessType.CODEX)

    assert str(path) in message
    assert len(message) < 1000
    # The user's own next message is the turn that should drive action.
    assert "Wait for the user's next message" in message
