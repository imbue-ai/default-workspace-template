"""Unit tests for the Claude model resolver."""

import json
from pathlib import Path

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.claude.model import ClaudeModelResolver
from imbue.system_interface.harnesses.model import EffortLevel
from imbue.system_interface.harnesses.model import ModelAxis
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


def test_switch_sends_only_the_axes_it_is_told(tmp_path: Path) -> None:
    # Told model + effort changed but not fast: /fast must NOT be sent.
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []

    def send(line: str) -> bool:
        sent.append(line)
        return True

    result = resolver.switch(
        ModelIdentity(model_id="sonnet", effort=EffortLevel.HIGH, fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        send,
    )

    assert result.ok
    assert sent == ["/model sonnet", "/effort high"]


def test_switch_fast_toggle_sends_only_fast(tmp_path: Path) -> None:
    # Only the fast axis changed: send just /fast on, not /model or /effort.
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="opus[1m]", effort=EffortLevel.MEDIUM, fast=True),
        frozenset({ModelAxis.FAST}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/fast on"]


def test_switch_reissues_a_value_the_agent_is_already_on(tmp_path: Path) -> None:
    # The reported bug: settings.json already says effort=medium, but the effort
    # axis is in the change set (the user went medium -> xhigh -> medium faster than
    # disk reconciled). The switch must still send /effort medium -- it applies the
    # axes it is told, never suppressing on a disk read.
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    _write_settings(tmp_path, {"model": "opus[1m]", "effortLevel": "medium"})
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="opus[1m]", effort=EffortLevel.MEDIUM, fast=False),
        frozenset({ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/effort medium"]


def test_switch_with_no_axes_sends_nothing(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="sonnet", effort=EffortLevel.HIGH, fast=False),
        frozenset(),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == []


def test_switch_reports_a_failed_send(tmp_path: Path) -> None:
    resolver = ClaudeModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="sonnet", effort=EffortLevel.HIGH, fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda _line: False,
    )
    assert not result.ok
    assert result.detail is not None
