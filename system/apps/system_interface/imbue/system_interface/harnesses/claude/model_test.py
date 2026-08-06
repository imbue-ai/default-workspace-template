"""Unit tests for the Claude model resolver."""

import json
from pathlib import Path

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.claude.model import ClaudeModelResolver
from imbue.system_interface.harnesses.model import EffortLevel
from imbue.system_interface.harnesses.model import ModelIdentity


def _agent_info(tmp_path: Path) -> AgentInfo:
    config_dir = tmp_path / "claude_config"
    config_dir.mkdir()
    (tmp_path / "state").mkdir()
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path / "state",
        claude_config_dir=config_dir,
    )


def _write_settings(tmp_path: Path, settings: dict[str, object]) -> None:
    (tmp_path / "claude_config" / "settings.json").write_text(json.dumps(settings))


def test_guess_from_launch_defaults_when_no_settings(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    guess = resolver.guess_from_launch()
    # The launch default: Opus (1M), medium effort, fast off (no settings written yet).
    assert guess == ModelIdentity(model_id="opus[1m]", effort=EffortLevel.MEDIUM, fast=False)


def test_read_live_is_none_until_settings_has_a_model(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    assert resolver.read_live() is None
    # A settings file with no model key still reads as nothing live.
    _write_settings(tmp_path, {"fastMode": True})
    assert resolver.read_live() is None


def test_read_live_reads_model_effort_and_fast(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    _write_settings(tmp_path, {"model": "sonnet", "effortLevel": "high", "fastMode": True})
    assert resolver.read_live() == ModelIdentity(model_id="sonnet", effort=EffortLevel.HIGH, fast=True)


def test_read_live_leaves_effort_none_before_first_effort(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    _write_settings(tmp_path, {"model": "opus[1m]"})
    live = resolver.read_live()
    assert live is not None
    assert live.effort is None


def test_switch_sends_the_three_commands_and_records_fast(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []

    def send(line: str) -> bool:
        sent.append(line)
        return True

    result = resolver.switch(ModelIdentity(model_id="sonnet", effort=EffortLevel.HIGH, fast=False), send)

    assert result.ok
    assert sent == ["/model sonnet", "/effort high", "/fast off"]


def test_switch_reports_a_failed_send(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(ModelIdentity(model_id="sonnet", effort=EffortLevel.HIGH, fast=False), lambda _line: False)
    assert not result.ok
    assert result.detail is not None
