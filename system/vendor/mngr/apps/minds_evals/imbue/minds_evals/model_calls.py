"""The forced-tool Anthropic call, shared by everything in this harness that makes one.

The goal-holding client (``decider``) and the UI-flow verification agent (``ui_flows``) ask very
different questions, but they ask them the same way -- one message, a fixed set of tools the model
must choose exactly one of, and a degraded answer rather than an exception when the API does not
come back. Keeping that call in one place is what stops the two from drifting apart on timeouts, on
which failures are swallowed, or on whether the model may answer with two tools at once.

The DECIDE_FROM_PERSONA role-play call is not one of these: it asks for plain text rather than a
tool, and keeps its ported error handling in ``decider``.
"""

from collections.abc import Sequence
from typing import Any
from typing import Final

import anthropic
from anthropic.types import ContentBlock
from anthropic.types import ToolChoiceParam
from anthropic.types import ToolParam
from anthropic.types import ToolUseBlock
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure


class ToolCallResult(FrozenModel):
    """One forced-tool call's outcome: which tool the model chose, its payload, and what it cost."""

    tool_name: str = Field(description="The tool the model called; empty when the call produced none")
    tool_input: dict[str, Any] | None = Field(description="The chosen tool's payload; None when there is none")
    input_token_count: int = Field(description="Input tokens the call consumed (0 when the call failed)")
    output_token_count: int = Field(description="Output tokens the call consumed (0 when the call failed)")


_NO_ANSWER: Final[ToolCallResult] = ToolCallResult(
    tool_name="", tool_input=None, input_token_count=0, output_token_count=0
)


@pure
def build_tool_choice(tool_names: Sequence[str]) -> ToolChoiceParam:
    """How the model is told to answer: the one tool by name, or any of several.

    Parallel tool use is disabled either way, so "exactly one" is enforced by the API rather than by
    the caller's reading order: a response carrying both a "keep going" tool and a "stop" tool would
    make stopping depend on which block happened to come first.
    """
    if len(set(tool_names)) == 1:
        return {"type": "tool", "name": tool_names[0], "disable_parallel_tool_use": True}
    return {"type": "any", "disable_parallel_tool_use": True}


@pure
def select_tool_use(content: Sequence[ContentBlock], tool_names: Sequence[str]) -> ToolUseBlock | None:
    """The first block in a response that answers with one of ``tool_names``, or None if none does.

    Tool inputs arrive as parsed JSON already; the payload guard is for a model that answered with
    something other than an object, which is no answer at all.
    """
    for block in content:
        if isinstance(block, ToolUseBlock) and block.name in tool_names and isinstance(block.input, dict):
            return block
    return None


def call_forced_tool(
    system_prompt: str,
    prompt: str,
    tools: Sequence[ToolParam],
    model: str,
    api_key: str,
    timeout_seconds: float,
    max_tokens: int,
    caller_label: str,
) -> ToolCallResult:
    """Ask the model one question it must answer by calling exactly one of ``tools``.

    A failure yields no payload rather than raising, because both callers can degrade -- the client
    sends its fallback line, the flow records an instrument failure -- and neither can afford to
    take a whole trial down over one flaky HTTP call.
    """
    tool_names = [tool["name"] for tool in tools]
    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds)
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=list(tools),
            tool_choice=build_tool_choice(tool_names),
            messages=[{"role": "user", "content": prompt}],
        )
    # Every failure mode the SDK raises -- transport, timeout, rate limit, a bad status -- shares
    # this base, and all of them mean the same thing here: no answer. Anything NOT from the SDK is a
    # bug in this module or its caller and is deliberately left to propagate.
    except anthropic.AnthropicError as exc:
        logger.warning("The {} model call failed: {}", caller_label, exc)
        return _NO_ANSWER
    block = select_tool_use(message.content, tool_names)
    if block is None:
        logger.warning("The {} model call named no known tool", caller_label)
    # The usage is reported either way: a model that answered with something unusable was still
    # billed for answering, and the caller's spend account is the only place that cost is measured.
    return ToolCallResult(
        tool_name="" if block is None else block.name,
        tool_input=None if block is None else block.input,
        input_token_count=message.usage.input_tokens,
        output_token_count=message.usage.output_tokens,
    )
