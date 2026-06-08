"""Frozen data types shared across the ai_integration library."""

from enum import auto

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field


class BillingPath(UpperCaseStrEnum):
    """Which backend (and therefore billing bucket) served a call.

    ``DIRECT_API`` -- the direct Anthropic API, billed pay-per-token against the
    API account (``ANTHROPIC_API_KEY``). ``CLAUDE_CLI`` -- headless ``claude -p``,
    which draws the separate programmatic / Agent-SDK pool on a subscription (or
    the API account if a key is present in its env). Neither competes with the
    interactive chat pool.
    """

    DIRECT_API = auto()
    CLAUDE_CLI = auto()


class Usage(FrozenModel):
    """Token counts for a single completion."""

    input_tokens: int = Field(description="Uncached input (prompt) tokens billed")
    output_tokens: int = Field(
        description="Generated output (completion) tokens billed"
    )
    cache_read_tokens: int = Field(
        default=0, description="Tokens served from the prompt cache (cheaper rate)"
    )
    cache_write_tokens: int = Field(
        default=0,
        description="Tokens written to the prompt cache (5-minute write rate)",
    )


class ToolCall(FrozenModel):
    """One ``tool_use`` block the model returned -- the structured-output channel.

    When a completion is steered to produce structured output (by passing ``tools``
    + ``tool_choice`` through ``anthropic_options``), the model answers with a
    ``tool_use`` block rather than text. ``input`` is the structured argument object
    the model produced (this is the JSON you actually want); ``text`` on the
    ``CompletionResult`` is typically empty in that case.
    """

    id: str = Field(description="The provider's id for this tool-use block")
    name: str = Field(description="The tool the model chose to call")
    input: dict[str, object] = Field(
        description="The structured arguments the model produced for the tool"
    )


class CompletionResult(FrozenModel):
    """The result of a non-agentic completion (``run_completion``)."""

    text: str = Field(description="The model's completion text")
    billing_path: BillingPath = Field(
        description="Which backend/billing bucket served the call"
    )
    model: str = Field(description="The model id the call was served by")
    tool_calls: tuple[ToolCall, ...] = Field(
        default=(),
        description=(
            "Structured-output tool_use blocks the model returned, in order; empty "
            "for an ordinary text completion. Read these (not ``text``) when you "
            "steered the call with tools/tool_choice via ``anthropic_options``."
        ),
    )
    usage: Usage | None = Field(
        default=None, description="Token counts, when the backend reported them"
    )
    cost_usd: float | None = Field(
        default=None,
        description=(
            "Actual cost when known (claude -p reports it; direct API is estimated "
            "from usage and the price table); None when it can't be determined"
        ),
    )


class AgentOutcome(UpperCaseStrEnum):
    """Normalized outcome of a launched full agent (``run_agent``)."""

    DONE = auto()
    STUCK = auto()
    NO_UPDATE_NEEDED = auto()
    TIMED_OUT = auto()
    UNKNOWN = auto()


class AgentResult(FrozenModel):
    """Structured result of a launched full agent."""

    outcome: AgentOutcome = Field(
        description="Normalized terminal outcome of the agent run"
    )
    report_type: str | None = Field(
        description="Raw ``type`` field from the worker's report frontmatter, if any"
    )
    report_name: str | None = Field(
        description="Raw ``name`` field from the worker's report frontmatter, if any"
    )
    body: str = Field(description="The prose the worker addressed to the user")
    branch: str | None = Field(
        default=None,
        description="The worker's git branch (survives agent teardown), if known",
    )
    raw_report: str = Field(
        default="", description="The verbatim report text the worker produced"
    )
