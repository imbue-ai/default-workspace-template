"""grok's model catalog, grok 1.0.62's built-in models, and its model resolver."""

import json
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.queue_tracker import GrokControlCommand
from imbue.chat.harnesses.grok.queue_tracker import GrokControlCommandKind
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.model import EffortChoice
from imbue.chat.harnesses.model import HarnessCatalog
from imbue.chat.harnesses.model import HarnessModelResolver
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.model import SwitchResult
from imbue.imbue_common.pure import pure

# `default_models.json` as the grok 1.0.62 binary embeds it.
BINARY_DEFAULT_MODELS_PATH: Final[Path] = Path(__file__).parent / "binary_default_models_v1_0_62.json"

# mngr writes grok's model_state.json at the agent state-dir root.
GROK_STATE_RELATIVE_PATH: Final[Path] = Path(".")


@pure
def build_catalog_options(default_models: Mapping[str, Any]) -> tuple[ModelOption, ...]:
    """One option per model in grok's default-models document, with the efforts it declares, lowest first."""
    return tuple(
        ModelOption(
            id=model["id"],
            label=model["name"],
            # grok declares a model's efforts highest first.
            efforts=tuple(
                EffortChoice(level=effort["value"]) for effort in reversed(model.get("reasoning_efforts", ()))
            ),
            default_effort=model.get("reasoning_effort"),
            supports_fast=False,
            harness_reported_model_id=model["model"] if model["model"] != model["id"] else None,
        )
        for model in default_models["models"]
    )


GROK_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=build_catalog_options(json.loads(BINARY_DEFAULT_MODELS_PATH.read_text())),
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_text="Powered by Grok Build",
    # The tap cancels the turn with one Ctrl+C and the flush worker types the held messages; nothing restarts.
    native_atomic_shoulder_tap_possible=True,
)


@pure
def is_control_command_in_force(command: GrokControlCommand, identity: ModelIdentity | None) -> bool:
    """Whether grok already runs what ``command`` asks for, by the identity its model_state.json records."""
    if identity is None:
        return False
    match command.kind:
        case GrokControlCommandKind.MODEL:
            option = next((option for option in GROK_CATALOG.options if option.id == command.value), None)
            reported_model_id = option.harness_reported_model_id if option is not None else None
            return identity.model_id in (command.value, reported_model_id)
        case GrokControlCommandKind.EFFORT:
            return identity.effort == command.value
        case _ as unreachable:
            assert_never(unreachable)


class GrokModelResolver(HarnessModelResolver):
    """Switches a grok agent through its control lane."""

    _agent_state_dir: Path

    @classmethod
    def build(cls, agent_info: AgentInfo) -> "GrokModelResolver":
        self = cls.__new__(cls)
        self._agent_state_dir = agent_info.agent_state_dir
        return self

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        # Not typed through ``send``: the flush worker is grok's only typist.
        queue = get_tracker(self._agent_state_dir)
        if ModelAxis.MODEL in axes:
            queue.hold_control_command(GrokControlCommand(kind=GrokControlCommandKind.MODEL, value=identity.model_id))
        if ModelAxis.EFFORT in axes and identity.effort is not None:
            queue.hold_control_command(GrokControlCommand(kind=GrokControlCommandKind.EFFORT, value=identity.effort))
        return SwitchResult(ok=True)
