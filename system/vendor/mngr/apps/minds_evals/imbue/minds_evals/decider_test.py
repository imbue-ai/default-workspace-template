from typing import Any

import pytest
from inline_snapshot import snapshot

from imbue.minds_evals.data_types import Transcript
from imbue.minds_evals.decider import FALLBACK_MESSAGE
from imbue.minds_evals.decider import SATISFIED_TOOL_NAME
from imbue.minds_evals.decider import SEND_MESSAGE_TOOL_NAME
from imbue.minds_evals.decider import build_decider_prompt
from imbue.minds_evals.decider import build_goal_prompt
from imbue.minds_evals.decider import decide_goal_action
from imbue.minds_evals.decider import decide_next_message
from imbue.minds_evals.decider import decider_result_from_text
from imbue.minds_evals.decider import goal_decision_from_call
from imbue.minds_evals.decider import parse_goal_tool_use
from imbue.minds_evals.decider import render_client_conversation
from imbue.minds_evals.model_calls import ToolCallResult


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
    # No call was made, so there is nothing to bill and no model to attribute it to.
    assert result.model == ""
    assert result.input_token_count == 0
    assert result.output_token_count == 0


def test_decider_result_from_text_reads_the_clients_line_and_its_usage() -> None:
    result = decider_result_from_text("  Can I see it yet?  ", "claude-opus-4-8", 120, 9)

    assert not result.is_fallback
    assert result.message == "Can I see it yet?"
    assert (result.input_token_count, result.output_token_count) == (120, 9)


@pytest.mark.parametrize("text", ["", "   \n "])
def test_decider_result_from_text_still_bills_a_call_that_said_nothing(text: str) -> None:
    """A model that answered was billed for answering, however empty the answer. Dropping that usage
    would hide the harness's own spend in exactly the runs where someone goes looking at it."""
    result = decider_result_from_text(text, "claude-opus-4-8", 430, 17)

    assert result.is_fallback
    assert result.message == FALLBACK_MESSAGE
    assert result.model == "claude-opus-4-8"
    assert (result.input_token_count, result.output_token_count) == (430, 17)


def test_build_goal_prompt_carries_the_persona_the_goal_and_the_conversation() -> None:
    prompt = build_goal_prompt("Busy founder", "See the app running", "AGENT: nearly there")

    assert "Your persona: Busy founder" in prompt
    assert "See the app running" in prompt
    assert "AGENT: nearly there" in prompt


def test_build_goal_prompt_names_an_empty_conversation_rather_than_leaving_a_blank() -> None:
    """A blank section reads as a truncated prompt; the model must be told the client speaks first."""
    assert "(nothing said yet)" in build_goal_prompt("", "See it running", "")


def test_parse_goal_tool_use_reads_a_message_and_its_usage() -> None:
    decision = parse_goal_tool_use(
        SEND_MESSAGE_TOOL_NAME,
        {"text": "  Can I see it yet?  "},
        "claude-opus-4-8",
        input_token_count=120,
        output_token_count=9,
    )

    assert decision is not None
    assert not decision.is_satisfied
    assert decision.call.message == "Can I see it yet?"
    assert decision.call.input_token_count == 120
    assert decision.call.output_token_count == 9
    assert not decision.call.is_fallback


def test_parse_goal_tool_use_reads_satisfaction_and_sends_nothing() -> None:
    decision = parse_goal_tool_use(
        SATISFIED_TOOL_NAME,
        {"reason": "The agent gave me a working link."},
        "claude-opus-4-8",
        input_token_count=100,
        output_token_count=5,
    )

    assert decision is not None
    assert decision.is_satisfied
    assert decision.satisfaction_reason == "The agent gave me a working link."
    # Satisfaction is recorded, never sent: a closing pleasantry costs a full agent turn.
    assert decision.call.message == ""


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        (SEND_MESSAGE_TOOL_NAME, {"text": "   "}),
        (SEND_MESSAGE_TOOL_NAME, {}),
        (SATISFIED_TOOL_NAME, {"reason": ""}),
        ("some_other_tool", {"text": "hello"}),
        # A payload is model output, free to ignore the schema; coercing these with `str()` would
        # send "['a', 'b']" or "123" to a real agent as the client's line.
        (SEND_MESSAGE_TOOL_NAME, {"text": ["a", "b"]}),
        (SEND_MESSAGE_TOOL_NAME, {"text": 123}),
        (SATISFIED_TOOL_NAME, {"reason": {"it": "works"}}),
    ],
)
def test_parse_goal_tool_use_rejects_a_payload_it_cannot_act_on(tool_name: str, tool_input: dict[str, Any]) -> None:
    """An empty message would waste a full agent turn and an unnamed reason records nothing, so both
    are treated as no answer (which sends the client down the fallback path) rather than coerced."""
    assert parse_goal_tool_use(tool_name, tool_input, "claude-opus-4-8", 1, 1) is None


def test_decide_goal_action_falls_back_without_api_key() -> None:
    decision = decide_goal_action(
        persona="",
        goal="See the app running",
        transcript=Transcript(events=()),
        model="claude-opus-4-8",
        api_key="",
    )

    assert not decision.is_satisfied
    assert decision.call.is_fallback
    assert decision.call.message == FALLBACK_MESSAGE
    # No call was made, so there is nothing to bill and no model to attribute it to -- the same
    # degraded shape the role-play client reports for its own keyless path.
    assert decision.call.model == ""
    assert (decision.call.input_token_count, decision.call.output_token_count) == (0, 0)


def test_goal_decision_from_call_reads_a_usable_answer() -> None:
    decision = goal_decision_from_call(
        ToolCallResult(
            tool_name=SEND_MESSAGE_TOOL_NAME,
            tool_input={"text": "Can I see it yet?"},
            input_token_count=120,
            output_token_count=9,
        ),
        "claude-opus-4-8",
    )

    assert decision.call.message == "Can I see it yet?"
    assert not decision.call.is_fallback


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        # The model answered but named no tool this client offers.
        ("", None),
        # The model named a tool and sent nothing usable in it.
        (SEND_MESSAGE_TOOL_NAME, {"text": "   "}),
    ],
)
def test_goal_decision_from_call_still_bills_an_answer_it_cannot_act_on(
    tool_name: str,
    tool_input: dict[str, str] | None,
) -> None:
    """A model that answered was billed for answering, however unusable the answer. Dropping that
    usage would hide the harness's own spend in exactly the runs where someone goes looking at it --
    the goal client can make one such call per exchange."""
    decision = goal_decision_from_call(
        ToolCallResult(tool_name=tool_name, tool_input=tool_input, input_token_count=430, output_token_count=17),
        "claude-opus-4-8",
    )

    assert decision.call.is_fallback
    assert decision.call.message == FALLBACK_MESSAGE
    assert decision.call.model == "claude-opus-4-8"
    assert (decision.call.input_token_count, decision.call.output_token_count) == (430, 17)
