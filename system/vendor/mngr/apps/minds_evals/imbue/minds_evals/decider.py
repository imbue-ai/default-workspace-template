"""The DECIDE_FROM_PERSONA role-play call: the simulated user's side of a persona conversation.

Renders the persona plus the transcript so far into the role-play prompt, asks the decider model for
the client's single next casual line, and falls back to the literal "Sounds good." on any error so a
flaky API call never stalls an eval run.
"""

from typing import Final

import anthropic
from anthropic.types import TextBlock
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import DeciderResult
from imbue.minds_evals.data_types import Transcript

DEFAULT_DECIDER_MODEL: Final[str] = "claude-opus-4-8"
DECIDER_MAX_TOKENS: Final[int] = 64
FALLBACK_MESSAGE: Final[str] = "Sounds good."


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
def _fallback_result() -> DeciderResult:
    return DeciderResult(
        message=FALLBACK_MESSAGE,
        model="",
        input_token_count=0,
        output_token_count=0,
        is_fallback=True,
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
        return _fallback_result()
    text = "".join(block.text for block in message.content if isinstance(block, TextBlock)).strip()
    if not text:
        logger.warning("Falling back to {!r}: decider returned no text", FALLBACK_MESSAGE)
        return _fallback_result()
    return DeciderResult(
        message=text,
        model=model,
        input_token_count=message.usage.input_tokens,
        output_token_count=message.usage.output_tokens,
        is_fallback=False,
    )
