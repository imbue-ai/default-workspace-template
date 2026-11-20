"""grok's model catalog, grok 1.0.62's built-in models, and its model resolver."""

import json
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger

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
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure

# `default_models.json` as the grok 1.0.62 binary embeds it.
BINARY_DEFAULT_MODELS_PATH: Final[Path] = Path(__file__).parent / "binary_default_models_v1_0_62.json"

# mngr writes grok's model_state.json at the agent state-dir root.
GROK_STATE_RELATIVE_PATH: Final[Path] = Path(".")

GROK_MODELS_COMMAND: Final[tuple[str, ...]] = ("grok", "models")
_LIST_MODELS_TIMEOUT_SECONDS: Final[float] = 15.0
# What `grok models` prints before its listing, one `  * <id> (default)` or `  - <id>` line per model.
_AVAILABLE_MODELS_HEADER: Final[str] = "Available models:"
_LISTED_MODEL_MARKERS: Final[tuple[str, ...]] = ("* ", "- ")
_DEFAULT_MODEL_SUFFIX: Final[str] = " (default)"


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


@pure
def parse_grok_models_output(output: str) -> tuple[str, ...]:
    """The model ids `grok models` lists, in its order; empty when it lists none."""
    lines = [line.strip() for line in output.splitlines()]
    if _AVAILABLE_MODELS_HEADER not in lines:
        return ()
    model_ids: list[str] = []
    for line in lines[lines.index(_AVAILABLE_MODELS_HEADER) + 1 :]:
        if not line.startswith(_LISTED_MODEL_MARKERS):
            break
        model_ids.append(line[len(_LISTED_MODEL_MARKERS[0]) :].removesuffix(_DEFAULT_MODEL_SUFFIX).strip())
    return tuple(model_ids)


def run_grok_models(environment: Mapping[str, str]) -> FinishedProcess:
    """Run `grok models` with ``environment`` as its whole environment."""
    return run_local_command_modern_version(
        GROK_MODELS_COMMAND,
        is_checked=False,
        timeout=_LIST_MODELS_TIMEOUT_SECONDS,
        env=environment,
        name="grok models",
    )


def list_grok_model_ids(
    environment: Mapping[str, str],
    run_models_command: Callable[[Mapping[str, str]], FinishedProcess] = run_grok_models,
) -> tuple[str, ...] | None:
    """The model ids `grok models` lists under ``environment``, or None when it gave no answer."""
    try:
        finished = run_models_command(environment)
    except ProcessSetupError as e:
        logger.warning("grok models could not start: {}", e)
        return None
    if finished.is_timed_out or finished.returncode != 0:
        logger.warning(
            "grok models failed (timed_out={}, returncode={}): {}",
            finished.is_timed_out,
            finished.returncode,
            finished.stderr.strip(),
        )
        return None
    return parse_grok_models_output(finished.stdout)


@pure
def offered_catalog_options(listed_model_ids: tuple[str, ...] | None) -> tuple[ModelOption, ...] | None:
    """The catalog options a listing names, in catalog order, or None (the whole catalog) when it names none."""
    if listed_model_ids is None:
        return None
    offered = tuple(option for option in GROK_CATALOG.options if option.id in listed_model_ids)
    return offered or None


def _no_account_environment() -> Mapping[str, str] | None:
    return None


class GrokModelResolver(HarnessModelResolver):
    """Switches a grok agent through its control lane, and offers the catalog models its account lists."""

    _agent_state_dir: Path
    # The whole environment of the agent's account to run `grok models` under, or None to offer the whole catalog.
    _read_account_environment: Callable[[], Mapping[str, str] | None]
    _run_models_command: Callable[[Mapping[str, str]], FinishedProcess]

    @classmethod
    def build(
        cls,
        agent_info: AgentInfo,
        read_account_environment: Callable[[], Mapping[str, str] | None] = _no_account_environment,
        run_models_command: Callable[[Mapping[str, str]], FinishedProcess] = run_grok_models,
    ) -> "GrokModelResolver":
        self = cls.__new__(cls)
        self._agent_state_dir = agent_info.agent_state_dir
        self._read_account_environment = read_account_environment
        self._run_models_command = run_models_command
        return self

    def list_offered_models(self) -> tuple[str, ...] | None:
        environment = self._read_account_environment()
        if environment is None:
            return None
        offered = offered_catalog_options(list_grok_model_ids(environment, self._run_models_command))
        return None if offered is None else tuple(option.id for option in offered)

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        # Not typed through ``send``: the flush worker is grok's only typist.
        queue = get_tracker(self._agent_state_dir)
        if ModelAxis.MODEL in axes:
            queue.hold_control_command(GrokControlCommand(kind=GrokControlCommandKind.MODEL, value=identity.model_id))
        if ModelAxis.EFFORT in axes and identity.effort is not None:
            queue.hold_control_command(GrokControlCommand(kind=GrokControlCommandKind.EFFORT, value=identity.effort))
        return SwitchResult(ok=True)
