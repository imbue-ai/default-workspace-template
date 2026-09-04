"""The conversation so far, written down for the harness taking over.

A replacement agent starts with an empty context window. What it needs is what
the user would tell a colleague picking up a thread: who said what, what was
tried, where things stand. It gets that as a FILE plus a pointer to it in its
first message, not as a giant inlined prompt -- so the agent reads what it needs
when it needs it, the handover survives histories far larger than any context
window, and the first user turn stays the user's own words.

Rendering is deliberately harness-blind. The input is the common event schema
(see ``harnesses.events``), so every harness's history renders the same way and
no harness's transcript format leaks into the handover. Tool payloads are named
and sized but not inlined: the point of the summary is the SHAPE of the
conversation -- and a diff or a file read the new agent can re-do itself is the
least valuable thing to spend the handover on.

This is version 1 of the mechanism, chosen because it is honest: it says exactly
what happened, with no model in the loop to get it wrong. A generated summary
(from a dedicated internal account) fits behind the same interface later --
``build_handoff_context`` keeps its signature and the pointer in the first
message does not move.
"""

from pathlib import Path
from typing import Any

from loguru import logger as _loguru_logger

from imbue.system_interface.harnesses.events import SPECIAL_EVENT_TYPE
from imbue.system_interface.harnesses.harness_type import HarnessType

_HANDOFF_SUBDIR = Path("data") / ".state" / "handoff"
_CONTEXT_FILENAME = "context.md"

# Prose is quoted in full up to this length and elided beyond it. Generous, because a
# truncated user requirement is worse than a long file: the new agent reads this at its
# own pace and the cost of length is nearly zero, while the cost of losing the one
# sentence that stated the goal is the whole handover.
MAX_QUOTED_CHARS = 4000

# Events rendered, newest-last. A conversation longer than this is summarized from its
# most recent turns: the far past of a long chat is the part the user is least likely to
# be mid-thought about, and the file has to stay readable.
MAX_RENDERED_EVENTS = 400


def handoff_dir_for_workspace(workspace_dir: Path, operation_id: str) -> Path:
    """Where one switch's working files live, under the workspace's machine state."""
    return workspace_dir / _HANDOFF_SUBDIR / operation_id


def _elide(text: str) -> str:
    if len(text) <= MAX_QUOTED_CHARS:
        return text
    return f"{text[:MAX_QUOTED_CHARS]}\n\n[... {len(text) - MAX_QUOTED_CHARS} more characters ...]"


def _render_user_message(event: dict[str, Any]) -> str | None:
    content = event.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    # A hidden message has no DOM in the chat either (a model-bar command, the seeded
    # welcome), so quoting it here would hand over text the user never sees as theirs.
    if event.get("display") == "hidden":
        return None
    return f"### User\n\n{_elide(content.strip())}"


def _render_tool_call(call: dict[str, Any]) -> str:
    name = call.get("tool_name") or "tool"
    label = call.get("header_label") or ""
    caption = call.get("caption_label") or ""
    detail = " ".join(part for part in (label, caption) if part).strip()
    return f"- {name}: {detail}" if detail else f"- {name}"


def _render_assistant_message(event: dict[str, Any]) -> str | None:
    parts: list[str] = []
    text = event.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(_elide(text.strip()))
    calls = event.get("tool_calls")
    if isinstance(calls, list) and calls:
        rendered = [_render_tool_call(call) for call in calls if isinstance(call, dict)]
        if rendered:
            parts.append("Tool calls:\n" + "\n".join(rendered))
    if not parts:
        return None
    return "### Assistant\n\n" + "\n\n".join(parts)


def _render_tool_result(event: dict[str, Any]) -> str | None:
    """Only failures. A successful call's output is re-derivable and usually long; a
    failure is the load-bearing part of the history -- it says what did not work."""
    if not event.get("is_error"):
        return None
    name = event.get("tool_name") or "tool"
    snippet = event.get("error_snippet")
    if isinstance(snippet, str) and snippet.strip():
        return f"### Tool failure ({name})\n\n{_elide(snippet.strip())}"
    return f"### Tool failure ({name})"


def render_conversation(events: list[dict[str, Any]]) -> str:
    """The conversation as markdown: user turns, assistant turns, tool calls, failures.

    ``special`` events (turn boundaries) are dropped for the same reason the chat drops
    them -- they are bookkeeping, not conversation.
    """
    rendered: list[str] = []
    for event in events[-MAX_RENDERED_EVENTS:]:
        event_type = event.get("type")
        if event_type == SPECIAL_EVENT_TYPE:
            continue
        if event_type == "user_message":
            block = _render_user_message(event)
        elif event_type == "assistant_message":
            block = _render_assistant_message(event)
        elif event_type == "tool_result":
            block = _render_tool_result(event)
        else:
            block = None
        if block is not None:
            rendered.append(block)
    return "\n\n".join(rendered)


def build_handoff_context(
    events: list[dict[str, Any]],
    from_harness: HarnessType,
    to_harness: HarnessType,
) -> str:
    """The whole context file: a header saying what happened, then the conversation.

    The header exists because the new agent has to know it is a successor. Without it
    the file reads as an unexplained transcript, and the agent's first instinct is to
    re-answer the last question in it rather than to continue past it.
    """
    truncation_note = ""
    if len(events) > MAX_RENDERED_EVENTS:
        truncation_note = (
            f"\nOnly the most recent {MAX_RENDERED_EVENTS} of {len(events)} transcript entries are included below.\n"
        )
    return (
        "# Conversation so far\n"
        "\n"
        f"You are taking over an in-progress conversation that was running on {from_harness.value} "
        f"and is now running on {to_harness.value}. Everything below already happened: it is the "
        "user's history with your predecessor, not a new request. Read it for context, then answer "
        "the user's next message as a continuation of it.\n"
        "\n"
        "Tool outputs are not included -- only which tools ran, and any failures. Re-run anything "
        "you need to see for yourself.\n"
        f"{truncation_note}"
        "\n"
        "---\n"
        "\n"
        f"{render_conversation(events)}\n"
    )


def write_handoff_context(
    workspace_dir: Path,
    operation_id: str,
    events: list[dict[str, Any]],
    from_harness: HarnessType,
    to_harness: HarnessType,
) -> Path | None:
    """Write the context file for one switch and return its path, or None if it could not be written.

    A failure here is not fatal to the switch: the new agent then starts without the
    history, which is a worse handover but still a working chat -- whereas refusing the
    switch over an unwritable state directory would strand the user on a harness they
    asked to leave.
    """
    directory = handoff_dir_for_workspace(workspace_dir, operation_id)
    path = directory / _CONTEXT_FILENAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(build_handoff_context(events, from_harness, to_harness), encoding="utf-8")
    except OSError as e:
        _loguru_logger.opt(exception=e).warning("Failed to write the handoff context file at {}", path)
        return None
    return path


def build_handoff_first_message(context_path: Path, from_harness: HarnessType, to_harness: HarnessType) -> str:
    """What the replacement agent is told first: that it is a successor, and where to read.

    A pointer rather than the history itself, so this message stays short enough to leave
    the agent's whole context window for the work.
    """
    return (
        f"You are continuing a conversation that was running on {from_harness.value} and has just "
        f"been moved to {to_harness.value}. The history is in {context_path}. Read it before you "
        "reply, and treat it as your own prior turns rather than as something to summarize back "
        "to the user. Do not start over, do not greet the user again, and do not re-ask anything "
        "the history already answers. Wait for the user's next message before taking any action."
    )
