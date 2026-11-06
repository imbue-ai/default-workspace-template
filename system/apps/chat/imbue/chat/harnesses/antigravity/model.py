"""antigravity's model catalog and its resolver.

The bar drives agy with ``/model <slug>``, which switches the running session in place. agy
names a model at an effort by suffixing the effort to the base slug (``gemini-3.8-flash`` at
``low`` is ``gemini-3.8-flash-low``), so one command sets both axes. agy has no fast mode.

The command is never typed by the switch request itself. agy is driven by one typist -- the
watcher's flush worker (see ``queue_tracker.py``) -- which types it once no turn is open: a
command typed into a live turn lands in whatever agy is showing, which can be a dialog.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.antigravity.queue_tracker import get_agent_tracker
from imbue.chat.harnesses.model import EffortChoice
from imbue.chat.harnesses.model import HarnessCatalog
from imbue.chat.harnesses.model import HarnessModelResolver
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.model import SwitchResult

# This list must be updated when Google ships new Antigravity models.
#
# Pinning the agy BINARY does not pin this. `agy models` fetches the list over the network
# (it prints "Fetching available models..." first), so it is account- and server-side: Google
# can add or retire models with no version bump, and `update_policy = "NEVER"` does nothing
# to stop that.
#
# Captured from a signed-in agy 1.4.7 on 2026-10-26: `agy models` for the slugs, and
# `agy --model <slug> -p /effort --output-format json` for each model's adjustable efforts and
# the one a bare base slug starts at. Each row is (base slug, name, efforts, default effort).
# The name is what the statusline records as the model: agy's display name, with the effort
# suffix dropped for a model whose effort is adjustable ("Gemini 3.8 Flash (High)" is recorded
# as "Gemini 3.8 Flash" at "high"). A model agy reports no effort for keeps its whole name.
#
# A model missing from this list still renders, via `derived_option` below, but it is not
# offered in the picker. Re-run those commands in a signed-in workspace whenever agy is bumped.
_MODEL_ROWS: Final[tuple[tuple[str, str, tuple[str, ...], str | None], ...]] = (
    ("gemini-3.8-flash", "Gemini 3.8 Flash", ("low", "medium", "high"), "high"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)", (), None),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)", (), None),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", (), None),
)

# agy writes its live state at the agent state-dir root (the statusline command runs with
# MNGR_AGENT_STATE_DIR), so the shared reader/watch path needs no subdirectory.
ANTIGRAVITY_STATE_RELATIVE_PATH: Final[Path] = Path(".")

ANTIGRAVITY_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=tuple(
        # agy's statusline reports a display name, not the slug `/model` takes, so the name is
        # the reported id.
        ModelOption(
            id=slug,
            label=label,
            harness_reported_model_id=label,
            efforts=tuple(EffortChoice(level=level) for level in efforts),
            default_effort=default_effort,
            supports_fast=False,
        )
        for slug, label, efforts, default_effort in _MODEL_ROWS
    ),
    # The chip moves on the click and settles once the statusline reports the new model, which
    # for a pick made mid-turn is after the turn ends.
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_text="Powered by Antigravity",
    # agy's tap does not restart: it ends the turn with Escape and then delivers the block we
    # were holding, so the entries never blink out (contract E1). Codex's shape, not claude's.
    native_atomic_shoulder_tap_possible=True,
)

_CATALOG_SLUGS: Final[frozenset[str]] = frozenset(slug for slug, _, _, _ in _MODEL_ROWS)


def derived_option(identity: ModelIdentity) -> ModelOption:
    """An option for a model agy reports that is absent from :data:`_MODEL_ROWS`.

    agy reports a display name, so it is the label as it stands. The model's reported effort is
    its one declared effort, which matches the live read without offering an effort control that
    could name a level the model does not have. Not offered in the picker: it is a rendering
    fallback, so a model newer than the list (worst case, a new default) shows its name and
    still lets the user switch to a listed one.
    """
    return ModelOption(
        id=identity.model_id,
        label=identity.model_id,
        harness_reported_model_id=identity.model_id,
        efforts=(EffortChoice(level=identity.effort),) if identity.effort is not None else (),
        supports_fast=False,
        in_picker=False,
    )


def model_command(identity: ModelIdentity) -> str:
    """The agy slash command that puts a session on ``identity``'s model at its effort."""
    if identity.effort is None:
        return f"/model {identity.model_id}"
    return f"/model {identity.model_id}-{identity.effort}"


class AntigravityModelResolver(HarnessModelResolver):
    """Switches an agy agent by holding its ``/model`` command for the flush worker to type."""

    _agent_id: str
    _state_dir: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "AntigravityModelResolver":
        self = cls.__new__(cls)
        self._agent_id = agent_info.id
        self._state_dir = agent_info.agent_state_dir
        return self

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        # Not through ``send``, which types now whatever agy is doing (see the module docstring).
        if ModelAxis.MODEL not in axes and ModelAxis.EFFORT not in axes:
            return SwitchResult(ok=True)
        if identity.model_id not in _CATALOG_SLUGS:
            # A pick can only name an unlisted model through `derived_option`, which is in the
            # option set only while the agent reports that model at that effort, so the agent is
            # already on it. Its id is a display name, which `/model` would not accept.
            logger.debug("antigravity: {} already runs the unlisted model {!r}", self._agent_id, identity.model_id)
            return SwitchResult(ok=True)
        get_agent_tracker(self._agent_id, self._state_dir).request_model_command(model_command(identity))
        return SwitchResult(ok=True)
