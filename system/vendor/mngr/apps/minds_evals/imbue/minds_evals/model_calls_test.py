from typing import Any

import pytest
from anthropic.types import ContentBlock
from anthropic.types import TextBlock
from anthropic.types import ToolUseBlock

from imbue.minds_evals.model_calls import build_tool_choice
from imbue.minds_evals.model_calls import select_tool_use


def test_build_tool_choice_pins_a_single_tool_by_name() -> None:
    """The verification agent offers one tool and needs it called, not merely considered."""
    assert build_tool_choice(["take_action"]) == {
        "type": "tool",
        "name": "take_action",
        "disable_parallel_tool_use": True,
    }


def test_build_tool_choice_asks_for_any_of_several_tools_and_still_only_one() -> None:
    """A response carrying both the goal client's "keep going" and "stop" tools would make stopping
    depend on which block happened to come first, so parallel tool use is disabled here too and the
    API is what enforces "exactly one"."""
    assert build_tool_choice(["send_message", "satisfied"]) == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }


def _tool_use(name: str, tool_input: dict[str, Any]) -> ToolUseBlock:
    return ToolUseBlock(id="tool-{}".format(name), name=name, input=tool_input, type="tool_use")


def _tool_use_with_raw_input(name: str, tool_input: Any) -> ToolUseBlock:
    """A tool block whose payload is not an object at all.

    Built the way the SDK builds a response block -- without validation -- because that is the only
    way one reaches this code: the validating constructor refuses it.
    """
    return ToolUseBlock.model_construct(id="tool-{}".format(name), name=name, input=tool_input, type="tool_use")


def _text(text: str) -> TextBlock:
    return TextBlock(text=text, type="text", citations=None)


def test_select_tool_use_reads_past_prose_to_the_tool_the_model_called() -> None:
    """Models narrate before they act, so the answer is rarely the first block."""
    block = select_tool_use([_text("Let me ask."), _tool_use("send_message", {"text": "hi"})], ["send_message"])

    assert block is not None
    assert (block.name, block.input) == ("send_message", {"text": "hi"})


def test_select_tool_use_takes_the_first_of_the_offered_tools() -> None:
    content: list[ContentBlock] = [
        _tool_use("satisfied", {"reason": "done"}),
        _tool_use("send_message", {"text": "more"}),
    ]

    block = select_tool_use(content, ["send_message", "satisfied"])

    assert block is not None
    assert block.name == "satisfied"


@pytest.mark.parametrize(
    "content",
    [
        [],
        [_text("I would rather just talk.")],
        # A tool the caller never offered is not an answer to the question it asked.
        [_tool_use("some_other_tool", {"text": "hi"})],
        # A payload that is not an object carries no arguments to act on.
        [_tool_use_with_raw_input("send_message", "hi")],
        [_tool_use_with_raw_input("send_message", None)],
    ],
)
def test_select_tool_use_finds_nothing_in_a_response_it_cannot_act_on(content: list[ContentBlock]) -> None:
    """Every one of these degrades its caller -- the client to its fallback line, the verifier to a
    recorded instrument failure -- so none of them may be mistaken for an answer."""
    assert select_tool_use(content, ["send_message", "satisfied"]) is None
