"""Grok Build's model catalog and its model resolver.

The catalog is the pinned grok's built-in model set, each model with the reasoning efforts the binary declares for
it. The live read is the shared reader of the ``model_state.json`` mngr_grok's status line writes when grok's
session starts and within about a second of a ``/model`` or ``/effort``; until its first run the bar shows no
slots. A switch holds grok's own ``/model <id>`` and ``/effort <level>`` in the agent's control lane, which the
flush worker sends, each alone, once no top-level turn is in flight; grok applies each at once and keeps it in the
agent's ``config.toml`` across a restart.

Which of the catalog's models an account offers is asked of ``grok models`` under the account's environment. A
listing that fails, or names none of the catalog's models, offers the whole catalog.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.account_binding import AGENT_ENV_FILENAME
from imbue.chat.harnesses.grok.account_binding import GROK_MODELS_COMMAND
from imbue.chat.harnesses.grok.account_binding import GrokAccountBinding
from imbue.chat.harnesses.grok.account_binding import XAI_API_KEY_ENV_VAR
from imbue.chat.harnesses.grok.account_binding import is_grok_ambient_variable
from imbue.chat.harnesses.grok.account_binding import read_sole_api_key
from imbue.chat.harnesses.grok.queue_tracker import enqueue_control_command
from imbue.chat.harnesses.model import EffortChoice
from imbue.chat.harnesses.model import HarnessCatalog
from imbue.chat.harnesses.model import HarnessModelResolver
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.model import SwitchResult
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.pure import pure
from imbue.mngr.utils.env_utils import parse_env_file
from imbue.mngr_grok.api import GROK_AUTH_PATH_ENV_VAR
from imbue.mngr_grok.api import get_model_state_path

# The pinned grok 1.0.62's built-in models (`default_models.json` in the binary): id, name and declared efforts.
_BUILT_IN_MODELS: Final[tuple[tuple[str, str, tuple[str, ...]], ...]] = (
    ("grok-4.6", "Grok 4.6", ("low", "medium", "high", "xhigh")),
    ("grok-4.5", "Grok 4.5", ("low", "medium", "high")),
)
# The effort every built-in model runs at until one is picked.
_BUILT_IN_DEFAULT_EFFORT: Final[str] = "high"

GROK_CATALOG: Final[HarnessCatalog] = HarnessCatalog(
    options=tuple(
        ModelOption(
            id=model_id,
            label=label,
            efforts=tuple(EffortChoice(level=level) for level in efforts),
            default_effort=_BUILT_IN_DEFAULT_EFFORT,
            supports_fast=False,
        )
        for model_id, label, efforts in _BUILT_IN_MODELS
    ),
    switch_mode=SwitchMode.EAGER_THEN_RECONCILE,
    picker_mode=PickerMode.LIST,
    powered_by_text="Powered by Grok Build",
    native_atomic_shoulder_tap_possible=True,
)

# The directory, relative to the agent state dir, that mngr_grok's status line writes model_state.json in.
GROK_STATE_RELATIVE_PATH: Final[Path] = get_model_state_path(Path(".")).parent

# What `grok models` prints before its listing, one `  * <id> (default)` or `  - <id>` line per model.
_AVAILABLE_MODELS_HEADER: Final[str] = "Available models:"
_LISTED_MODEL_MARKERS: Final[tuple[str, ...]] = ("* ", "- ")
_DEFAULT_MODEL_SUFFIX: Final[str] = " (default)"
_LIST_MODELS_TIMEOUT_SECONDS: Final[float] = 30.0

# Holds one `/model` or `/effort` line for the agent's flush worker, which sends it alone.
HoldControlCommand = Callable[[Path, str], None]


@pure
def parse_grok_models_output(output: str) -> tuple[str, ...]:
    """The model ids ``grok models`` lists, in its order; empty when it lists none."""
    lines = [line.strip() for line in output.splitlines()]
    if _AVAILABLE_MODELS_HEADER not in lines:
        return ()
    model_ids: list[str] = []
    for line in lines[lines.index(_AVAILABLE_MODELS_HEADER) + 1 :]:
        if not line.startswith(_LISTED_MODEL_MARKERS):
            break
        model_ids.append(line[len(_LISTED_MODEL_MARKERS[0]) :].removesuffix(_DEFAULT_MODEL_SUFFIX).strip())
    return tuple(model_ids)


def build_grok_models_environment(account_dir: Path) -> dict[str, str]:
    """The server's environment without its own grok variables or xAI key, bound to the account.

    An account whose only credential is a stored API key gets it as ``XAI_API_KEY``: ``grok models`` lists
    nothing for a key it finds in ``auth.json``.
    """
    environment = {name: value for name, value in os.environ.items() if not is_grok_ambient_variable(name)}
    environment.update(GrokAccountBinding().account_env(account_dir))
    api_key = read_sole_api_key(account_dir)
    if api_key is not None:
        environment[XAI_API_KEY_ENV_VAR] = api_key.get_secret_value()
    return environment


def list_account_model_ids(
    account_dir: Path, runner: Callable[..., Any] = run_local_command_modern_version
) -> tuple[str, ...] | None:
    """The model ids ``grok models`` lists for the account, or None when it gave no answer."""
    try:
        finished = runner(
            command=list(GROK_MODELS_COMMAND),
            is_checked=False,
            timeout=_LIST_MODELS_TIMEOUT_SECONDS,
            cwd=None,
            env=build_grok_models_environment(account_dir),
            name="grok models",
        )
    except ProcessError as e:
        logger.warning("grok models could not run for account {}: {}", account_dir.name, e)
        return None
    if finished.is_timed_out or finished.returncode != 0:
        logger.warning(
            "grok models failed for account {} (timed_out={}, returncode={})",
            account_dir.name,
            finished.is_timed_out,
            finished.returncode,
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


def read_agent_account_dir(agent_state_dir: Path) -> Path | None:
    """The account folder the agent's env file binds it to, the folder of its ``GROK_AUTH_PATH``, or None."""
    env_path = agent_state_dir / AGENT_ENV_FILENAME
    try:
        env_vars = parse_env_file(env_path.read_text())
    except OSError as e:
        logger.debug("No account binding readable from {}: {}", env_path, e)
        return None
    auth_path = env_vars.get(GROK_AUTH_PATH_ENV_VAR)
    return Path(auth_path).parent if auth_path else None


class GrokModelResolver(HarnessModelResolver):
    """Switches a grok agent's model and effort through its control lane, and offers its account's models."""

    _agent_state_dir: Path
    _hold_control_command: HoldControlCommand
    _runner: Callable[..., Any]

    @classmethod
    def build(
        cls,
        agent_info: AgentInfo,
        hold_control_command: HoldControlCommand = enqueue_control_command,
        runner: Callable[..., Any] = run_local_command_modern_version,
    ) -> "GrokModelResolver":
        self = cls.__new__(cls)
        self._agent_state_dir = agent_info.agent_state_dir
        self._hold_control_command = hold_control_command
        self._runner = runner
        return self

    def list_offered_models(self) -> tuple[str, ...] | None:
        account_dir = read_agent_account_dir(self._agent_state_dir)
        if account_dir is None:
            return None
        offered = offered_catalog_options(list_account_model_ids(account_dir, self._runner))
        return None if offered is None else tuple(option.id for option in offered)

    @classmethod
    def list_account_options(
        cls, account_dir: Path, runner: Callable[..., Any] = run_local_command_modern_version
    ) -> tuple[ModelOption, ...] | None:
        return offered_catalog_options(list_account_model_ids(account_dir, runner))

    def switch(self, identity: ModelIdentity, axes: frozenset[ModelAxis], send: Callable[[str], bool]) -> SwitchResult:
        # ``send`` is unused: the flush worker is grok's only typist, and the bar reconciles from model_state.json.
        if ModelAxis.MODEL in axes:
            self._hold_control_command(self._agent_state_dir, f"/model {identity.model_id}")
        if ModelAxis.EFFORT in axes and identity.effort is not None:
            self._hold_control_command(self._agent_state_dir, f"/effort {identity.effort}")
        return SwitchResult(ok=True)
