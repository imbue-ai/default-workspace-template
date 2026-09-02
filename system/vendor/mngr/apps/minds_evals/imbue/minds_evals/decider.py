"""The simulated client's model calls: the DECIDE_FROM_PERSONA role-play and the goal-holding client
that keeps asking until it is satisfied.

Both render the persona plus the transcript so far into a prompt and ask the decider model what the
client says next, and neither raises its way out of a run: a call that comes back with nothing
usable falls back to the literal "Sounds good.", which the driver sends once, so a flaky API call
never stalls an eval run. The role-play call swallows any exception at all; the goal client swallows the failures the SDK raises and lets a bug in this module propagate.
"""

from collections.abc import Mapping
from typing import Any
from typing import Final

import anthropic
from anthropic.types import TextBlock
from anthropic.types import ToolParam
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.minds_evals import model_calls
from imbue.minds_evals.data_types import DeciderResult
from imbue.minds_evals.data_types import GoalDecision
from imbue.minds_evals.data_types import Transcript

DEFAULT_DECIDER_MODEL: Final[str] = "claude-opus-4-8"
DECIDER_MAX_TOKENS: Final[int] = 64
FALLBACK_MESSAGE: Final[str] = "Sounds good."

# The goal client writes a real message rather than a one-liner, and states a reason when it stops,
# so it gets more room than the role-play call.
GOAL_MAX_TOKENS: Final[int] = 512
# Generous, because a call that never comes back is expensive: the client degrades to its fallback
# line, which is sent once and ends the entry, forfeiting the rest of that entry's budget.
GOAL_CALL_TIMEOUT_SECONDS: Final[float] = 120.0

SEND_MESSAGE_TOOL_NAME: Final[str] = "send_message"
SATISFIED_TOOL_NAME: Final[str] = "satisfied"


@pure
def render_client_conversation(transcript: Transcript) -> str:
    """The user-facing conversation so far: user_message.content / non-empty assistant_message.text.

    The transcript here is the driver's own rendered conversation (``_conversation_events``), not the
    raw workspace event stream, so it carries these shapes whatever vintage the agent emitted."""
    lines: list[str] = []
    for event in transcript.events:
        if event.get("type") == "assistant_message":
            text = (event.get("text") or "").strip()
            if text:
                lines.append("AGENT: {}".format(text))
        elif event.get("type") == "user_message":
            content = (event.get("content") or "").strip()
            if content:
                lines.append("YOU (client): {}".format(content))
        else:
            # Internal events (tool use, thinking, harness markers) are not part
            # of the user-facing conversation.
            pass
    return "\n\n".join(lines)


@pure
def build_decider_prompt(persona: str, conversation: str) -> str:
    who = "You are the client in this conversation."
    if persona:
        who += " Your persona: {}".format(persona)
    return (
        "{who} An AI agent (AGENT) is building software for you. Below is the conversation so far.\n\n"
        "Reply with the single next thing you would casually say to keep it moving -- ONE short "
        "sentence or just a few words, in a natural, non-technical voice. Output only that message, "
        "nothing else.\n\nConversation so far:\n{conversation}"
    ).format(who=who, conversation=conversation)


@pure
def _fallback_result(model: str = "", input_token_count: int = 0, output_token_count: int = 0) -> DeciderResult:
    """The degraded result: the literal fallback line in place of the client's own.

    It carries the usage of whatever call produced it, because a model that answered with something
    unusable was still billed for answering; the harness's own spend account is the only place that
    cost is ever measured. A call that raised reports the model it was made against and no usage;
    the defaults are for the path where there was no call at all.
    """
    return DeciderResult(
        message=FALLBACK_MESSAGE,
        model=model,
        input_token_count=input_token_count,
        output_token_count=output_token_count,
        is_fallback=True,
    )


@pure
def decider_result_from_text(
    text: str,
    model: str,
    input_token_count: int,
    output_token_count: int,
) -> DeciderResult:
    """One completed role-play call's text as the client's next message, degrading to the fallback
    line when the model answered with nothing to say.

    Either way the result carries the usage the call reported: an answer the driver cannot send is
    still an answer the harness paid for.
    """
    line = text.strip()
    if not line:
        logger.warning("Falling back to {!r}: decider returned no text", FALLBACK_MESSAGE)
        return _fallback_result(
            model=model, input_token_count=input_token_count, output_token_count=output_token_count
        )
    return DeciderResult(
        message=line,
        model=model,
        input_token_count=input_token_count,
        output_token_count=output_token_count,
        is_fallback=False,
    )


def decide_next_message(
    persona: str,
    transcript: Transcript,
    model: str,
    api_key: str,
) -> DeciderResult:
    """Ask the decider model for the client's next casual line, falling back to 'Sounds good.' on any error."""
    if not api_key:
        logger.warning("Falling back to {!r}: no Anthropic API key for the decider", FALLBACK_MESSAGE)
        return _fallback_result()
    prompt = build_decider_prompt(persona, render_client_conversation(transcript))
    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=DECIDER_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    # Ported semantics: a role-play API hiccup (any error) must not stall the run.
    except Exception as exc:
        logger.warning("Falling back to {!r}: decider call failed ({})", FALLBACK_MESSAGE, exc)
        return _fallback_result(model=model)
    return decider_result_from_text(
        text="".join(block.text for block in message.content if isinstance(block, TextBlock)),
        model=model,
        input_token_count=message.usage.input_tokens,
        output_token_count=message.usage.output_tokens,
    )


_SEND_MESSAGE_TOOL: Final[ToolParam] = {
    "name": SEND_MESSAGE_TOOL_NAME,
    "description": "Say the next thing to the agent, because the goal is not met yet.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": ("What you say next, in your own non-technical voice: a couple of sentences at most."),
            },
        },
        "required": ["text"],
    },
}

_SATISFIED_TOOL: Final[ToolParam] = {
    "name": SATISFIED_TOOL_NAME,
    "description": "Stop asking: what you wanted out of this part of the conversation has been delivered.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "One sentence on what the agent did that met the goal.",
            },
        },
        "required": ["reason"],
    },
}

_GOAL_SYSTEM_PROMPT: Final[str] = (
    "You are a non-technical client talking to an AI agent that is building software for you. You "
    "have one thing you want out of this part of the conversation, and you keep the conversation "
    "going until you have it.\n\n"
    "Every exchange you either say the next thing (send_message) or declare yourself satisfied "
    "(satisfied). Choose exactly one.\n\n"
    "Rules:\n"
    "- Judge satisfaction from the conversation alone. You cannot open the app, read files, or run "
    "anything -- you only know what the agent has told you.\n"
    "- Speak like a client, not an engineer: no jargon, no implementation instructions, no test "
    "plans.\n"
    "- Do not repeat yourself. If the agent has answered part of your goal, push on the part it has "
    "not.\n"
    "- Declare yourself satisfied as soon as the agent says the goal is met; do not keep going for "
    "reassurance, and do not send a closing pleasantry -- the reason you give is recorded."
)


@pure
def build_goal_prompt(persona: str, goal: str, conversation: str) -> str:
    """The goal client's prompt: who it is, what it is holding out for, and the conversation so far."""
    who = "You are the client in this conversation."
    if persona:
        who += " Your persona: {}".format(persona)
    return (
        "{who}\n\nWhat you want out of this part of the conversation:\n{goal}\n\nConversation so far:\n{conversation}"
    ).format(who=who, goal=goal, conversation=conversation or "(nothing said yet)")


@pure
def _goal_fallback_decision(model: str = "", input_token_count: int = 0, output_token_count: int = 0) -> GoalDecision:
    """The degraded decision: send the fallback line and let the caller end the entry.

    Not satisfied, because nothing was learned about the goal. The degraded result itself is the
    role-play call's, so the two model-backed clients cannot disagree about what one looks like.
    """
    return GoalDecision(
        is_satisfied=False,
        satisfaction_reason="",
        call=_fallback_result(
            model=model,
            input_token_count=input_token_count,
            output_token_count=output_token_count,
        ),
    )


@pure
def _tool_text(tool_input: Mapping[str, Any], key: str) -> str:
    """One string field of a forced-tool payload; empty when it is absent, blank, or not a string."""
    value = tool_input.get(key)
    return value.strip() if isinstance(value, str) else ""


@pure
def parse_goal_tool_use(
    tool_name: str,
    tool_input: Mapping[str, Any],
    model: str,
    input_token_count: int,
    output_token_count: int,
) -> GoalDecision | None:
    """One forced-tool payload into a decision, or None when it carries nothing usable.

    An empty message or an empty reason is treated as no answer rather than coerced: sending an
    empty client message would waste a full agent turn, and a satisfaction with no reason records
    nothing for the judge to read. So is a field that is not a string at all: a payload is model
    output, free to ignore the schema, and `str()` would turn a list into "['a', 'b']" and send
    that to a real agent as the client's line.
    """
    if tool_name == SATISFIED_TOOL_NAME:
        reason = _tool_text(tool_input, "reason")
        message_text = ""
        is_satisfied = True
    elif tool_name == SEND_MESSAGE_TOOL_NAME:
        reason = ""
        message_text = _tool_text(tool_input, "text")
        is_satisfied = False
    else:
        return None
    if not (reason or message_text):
        return None
    return GoalDecision(
        is_satisfied=is_satisfied,
        satisfaction_reason=reason,
        call=DeciderResult(
            message=message_text,
            model=model,
            input_token_count=input_token_count,
            output_token_count=output_token_count,
            is_fallback=False,
        ),
    )


def decide_goal_action(
    persona: str,
    goal: str,
    transcript: Transcript,
    model: str,
    api_key: str,
) -> GoalDecision:
    """One exchange of the goal-holding client: say the next thing, or declare the goal met.

    Every way the call can fail to produce an answer -- no key, a failure the SDK raises, a response
    that named no tool, a tool payload with nothing in it -- yields the literal fallback message,
    which the caller sends once before ending the entry, so a flaky API never wedges a trial. A bug
    in this module is not one of those ways: it raises, exactly as it would anywhere else.
    """
    if not api_key:
        logger.warning("Falling back to {!r}: no Anthropic API key for the goal client", FALLBACK_MESSAGE)
        return _goal_fallback_decision()
    call = model_calls.call_forced_tool(
        system_prompt=_GOAL_SYSTEM_PROMPT,
        prompt=build_goal_prompt(persona, goal, render_client_conversation(transcript)),
        tools=[_SEND_MESSAGE_TOOL, _SATISFIED_TOOL],
        model=model,
        api_key=api_key,
        timeout_seconds=GOAL_CALL_TIMEOUT_SECONDS,
        max_tokens=GOAL_MAX_TOKENS,
        caller_label="goal client",
    )
    return goal_decision_from_call(call, model)


def goal_decision_from_call(call: model_calls.ToolCallResult, model: str) -> GoalDecision:
    """One forced-tool call's result as the client's decision, degrading to the fallback line when
    the call came back with nothing this client can act on.

    Either way the decision carries the usage the call reported: a model that answered with an
    unknown tool or an empty payload was still billed for answering, and a call the SDK failed
    reports zeros of its own, so neither path has to ask which kind of failure it was.
    """
    if call.tool_input is None:
        logger.warning("Falling back to {!r}: the goal client produced no decision", FALLBACK_MESSAGE)
        return _goal_fallback_decision(
            model=model,
            input_token_count=call.input_token_count,
            output_token_count=call.output_token_count,
        )
    decision = parse_goal_tool_use(
        tool_name=call.tool_name,
        tool_input=call.tool_input,
        model=model,
        input_token_count=call.input_token_count,
        output_token_count=call.output_token_count,
    )
    if decision is None:
        logger.warning("Falling back to {!r}: the goal client's tool payload was empty", FALLBACK_MESSAGE)
        return _goal_fallback_decision(
            model=model,
            input_token_count=call.input_token_count,
            output_token_count=call.output_token_count,
        )
    return decision
