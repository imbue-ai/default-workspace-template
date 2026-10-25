"""Exercise launch selections through the installed harness configuration code."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from mngr_cli_contract.contract import assert_mngr_argv_valid

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.registry import build_model_launch
from imbue.mngr.cli.common_opts import apply_settings_to_config
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_claude.plugin import ClaudeCoreAgent
from imbue.mngr_codex.codex_config import build_codex_config
from imbue.mngr_codex.plugin import CodexAgentConfig

_MODEL_AND_EFFORT = frozenset({ModelAxis.MODEL, ModelAxis.EFFORT})


class _RecordingHost:
    def execute_idempotent_command(self, command: str, *, timeout_seconds: float) -> None:
        assert command.startswith("mkdir -p ")

    def write_text_file(self, path: Path, content: str) -> None:
        path.write_text(content)


def test_claude_seeds_its_first_model_state_from_the_selected_launch_settings(tmp_path: Path) -> None:
    launch = build_model_launch(
        HarnessType.CLAUDE, ModelIdentity(model_id="sonnet[1m]", effort="high", fast=False), _MODEL_AND_EFFORT
    )
    assert launch is not None
    base = apply_settings_to_config(
        MngrConfig(),
        [
            'agent_types.claude.settings_overrides.model="opus[1m]"',
            "agent_types.claude.settings_overrides.fastMode=true",
            "agent_types.claude.settings_overrides.skipDangerousModePermissionPrompt=true",
        ],
        frozenset(),
    )
    config = apply_settings_to_config(base, launch.settings, frozenset()).agent_types["claude"]
    assert isinstance(config, ClaudeAgentConfig)
    assert config.settings_overrides["skipDangerousModePermissionPrompt"] is True
    agent = SimpleNamespace(agent_config=config, _get_agent_dir=lambda: tmp_path)
    ClaudeCoreAgent._seed_model_state(cast(ClaudeCoreAgent, agent), cast(OnlineHostInterface, _RecordingHost()))
    initial = json.loads((tmp_path / "model_state.json").read_text())
    assert initial == {"model": "sonnet[1m]", "effort": "high", "fast": False}


def test_a_remembered_opus_selection_keeps_the_new_chats_speed_default() -> None:
    launch = build_model_launch(
        HarnessType.CLAUDE, ModelIdentity(model_id="opus[1m]", effort="max", fast=False), _MODEL_AND_EFFORT
    )
    assert launch is not None
    base = apply_settings_to_config(MngrConfig(), ["agent_types.claude.settings_overrides.fastMode=true"], frozenset())
    config = apply_settings_to_config(base, launch.settings, frozenset()).agent_types["claude"]
    assert isinstance(config, ClaudeAgentConfig)
    assert config.settings_overrides["fastMode"] is True
    assert config.settings_overrides["effortLevel"] == "max"


@pytest.mark.parametrize("is_explicit", [False, True])
def test_codex_writes_the_selected_model_and_effort_into_its_initial_config(is_explicit: bool) -> None:
    axes = frozenset(ModelAxis) if is_explicit else _MODEL_AND_EFFORT
    launch = build_model_launch(
        HarnessType.CODEX, ModelIdentity(model_id="gpt-6-astra", effort="max", fast=False), axes
    )
    assert launch is not None
    base = apply_settings_to_config(
        MngrConfig(),
        [
            'agent_types.codex.model="gpt-6-sol"',
            'agent_types.codex.config_overrides.service_tier="priority"',
        ],
        frozenset(),
    )
    config = apply_settings_to_config(base, launch.settings, frozenset()).agent_types["codex"]
    assert isinstance(config, CodexAgentConfig)
    initial = build_codex_config(
        model=config.model,
        model_reasoning_effort=config.model_reasoning_effort,
        sandbox_mode=config.sandbox_mode,
        approval_policy=None,
        trusted_projects=(),
        config_overrides=config.config_overrides,
    )
    assert initial["model"] == "gpt-6-astra"
    assert initial["model_reasoning_effort"] == "max"
    assert initial["service_tier"] == ("default" if is_explicit else "priority")


@pytest.mark.parametrize(
    "harness,model",
    [
        (HarnessType.CLAUDE, "sonnet[1m]"),
        (HarnessType.CODEX, "gpt-6-astra"),
        (HarnessType.PI_CODING, "anthropic/claude-sonnet-5"),
    ],
)
def test_model_launch_arguments_are_accepted_by_the_installed_creator(harness: HarnessType, model: str) -> None:
    launch = build_model_launch(harness, ModelIdentity(model_id=model, effort="high", fast=False), _MODEL_AND_EFFORT)
    assert launch is not None
    argv = ["mngr", "create", "launch-check", "--type", harness]
    for setting in launch.settings:
        argv.extend(["-S", setting])
    if launch.agent_args:
        argv.extend(["--", *launch.agent_args])
    assert_mngr_argv_valid(argv)
