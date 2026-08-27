from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# A prompts entry equal to this sentinel is role-played by the decider model
# instead of being sent verbatim. It cannot be the first prompt (there is no
# transcript to decide from yet).
DECIDE_SENTINEL: Final[str] = "DECIDE_FROM_PERSONA"

# The default-workspace-template (dwt) each eval case is cloned from.
DEFAULT_DWT_REPO: Final[str] = "https://github.com/imbue-ai/default-workspace-template.git"
DEFAULT_DWT_BRANCH: Final[str] = "main"

DEFAULT_TIMEOUT_SECONDS: Final[float] = 3600.0

# Seed value for the wordiness guard until PR2 measures real old-harness batch
# averages; overridable per eval config via "avg_word_count_baseline".
DEFAULT_AVG_WORD_COUNT_BASELINE: Final[float] = 120.0


class PersonaCase(FrozenModel):
    """One persona case from an eval config: an id, an optional persona, and one prompt per turn."""

    case_id: str = Field(description="Stable case id; names the task directory and the trial")
    persona: str = Field(description="Client persona role-played on DECIDE_FROM_PERSONA turns (may be empty)")
    prompts: tuple[str, ...] = Field(description="One entry per turn: a literal message or DECIDE_FROM_PERSONA")


class EvalConfig(FrozenModel):
    """A validated eval config file: the mngr branch under test plus the persona cases."""

    mngr_branch: str = Field(description="The mngr branch the box is built from")
    dwt_repo: str = Field(description="Workspace template repo each case is cloned from")
    dwt_branch: str = Field(description="Workspace template branch")
    timeout_seconds: float = Field(description="Per-case wall-clock budget in seconds")
    avg_word_count_baseline: float = Field(description="Baseline for the verifier's wordiness guard")
    cases: tuple[PersonaCase, ...] = Field(description="The persona cases, one task each")


class CaseConfig(FrozenModel):
    """The full per-case config carried in the task instruction and in tests/case.json."""

    case_id: str = Field(description="Stable case id")
    persona: str = Field(description="Client persona for DECIDE_FROM_PERSONA turns (may be empty)")
    prompts: tuple[str, ...] = Field(description="One entry per turn")
    timeout_seconds: float = Field(description="Per-case wall-clock budget in seconds")
    mngr_branch: str = Field(description="The mngr branch the box was built from")
    mngr_sha: str = Field(description="Exact mngr SHA resolved at generation time")
    dwt_repo: str = Field(description="Workspace template repo")
    dwt_branch: str = Field(description="Workspace template branch the SHA was resolved from")
    dwt_sha: str = Field(description="Exact workspace template SHA resolved at generation time")
    avg_word_count_baseline: float = Field(description="Baseline for the verifier's wordiness guard")


class Transcript(FrozenModel):
    """The conversation so far, as raw system_interface events (verbatim schema)."""

    events: tuple[dict[str, Any], ...] = Field(description="Raw events from the workspace system_interface")


class DeciderResult(FrozenModel):
    """One decider (simulated-user) model call: the message plus usage accounting."""

    message: str = Field(description="The client's next message")
    model: str = Field(description="The decider model used (empty when the fallback was used)")
    input_token_count: int = Field(description="Input tokens consumed by the call (0 on fallback)")
    output_token_count: int = Field(description="Output tokens consumed by the call (0 on fallback)")
    is_fallback: bool = Field(description="Whether the literal fallback message was used")
