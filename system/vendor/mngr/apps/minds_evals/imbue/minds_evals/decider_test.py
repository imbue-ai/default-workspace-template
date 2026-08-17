from inline_snapshot import snapshot

from imbue.minds_evals.data_types import Transcript
from imbue.minds_evals.decider import FALLBACK_MESSAGE
from imbue.minds_evals.decider import build_decider_prompt
from imbue.minds_evals.decider import decide_next_message
from imbue.minds_evals.decider import render_client_conversation


def test_render_client_conversation_labels_user_and_agent_turns() -> None:
    transcript = Transcript(
        events=(
            {"type": "user_message", "content": "Build me a thing"},
            {"type": "assistant_message", "text": "On it."},
            {"type": "assistant_message", "text": ""},
            {"type": "tool_use", "name": "bash"},
            {"type": "user_message", "content": "Sounds good."},
        )
    )

    rendered = render_client_conversation(transcript)

    assert rendered == snapshot("""\
YOU (client): Build me a thing

AGENT: On it.

YOU (client): Sounds good.\
""")


def test_render_client_conversation_is_empty_for_no_events() -> None:
    assert render_client_conversation(Transcript(events=())) == ""


def test_build_decider_prompt_includes_persona_when_present() -> None:
    prompt = build_decider_prompt("Busy founder", "AGENT: hi")

    assert "Your persona: Busy founder" in prompt
    assert "AGENT: hi" in prompt


def test_build_decider_prompt_omits_persona_when_empty() -> None:
    prompt = build_decider_prompt("", "AGENT: hi")

    assert "Your persona" not in prompt


def test_decide_next_message_falls_back_without_api_key() -> None:
    result = decide_next_message(
        persona="",
        transcript=Transcript(events=()),
        model="claude-opus-4-8",
        api_key="",
    )

    assert result.is_fallback
    assert result.message == FALLBACK_MESSAGE
    assert result.input_token_count == 0
    assert result.output_token_count == 0
