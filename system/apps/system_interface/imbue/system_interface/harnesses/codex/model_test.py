"""Unit tests for the Codex model resolver (read-only v1)."""

import json
from pathlib import Path

from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import EffortLevel
from imbue.system_interface.harnesses.model import ModelIdentity


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.CODEX,
    )


def _write_rollout(tmp_path: Path, thread_settings: dict[str, object]) -> None:
    """Write a rollout with a thread_settings_applied line and point the marker at it."""
    sessions = get_codex_home(tmp_path) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / "rollout.jsonl"
    line = {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_settings": thread_settings}}
    rollout.write_text(json.dumps(line) + "\n")
    (tmp_path / "codex_transcript_path").write_text(str(rollout))


def test_guess_from_launch_defaults_when_no_config(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.guess_from_launch() == ModelIdentity(model_id="gpt-5.6-sol", effort=EffortLevel.MEDIUM, fast=False)


def test_guess_from_launch_reads_config_toml(tmp_path: Path) -> None:
    config_path = get_codex_config_path(get_codex_home(tmp_path))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('model = "gpt-5.5"\nmodel_reasoning_effort = "low"\n')
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.guess_from_launch() == ModelIdentity(model_id="gpt-5.5", effort=EffortLevel.LOW, fast=False)


def test_read_live_is_none_before_any_turn(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.read_live() is None


def test_read_live_parses_the_last_thread_settings(tmp_path: Path) -> None:
    _write_rollout(tmp_path, {"model": "gpt-5.6-sol", "reasoning_effort": "high", "service_tier": "priority"})
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    # priority service tier reads as fast on.
    assert resolver.read_live() == ModelIdentity(model_id="gpt-5.6-sol", effort=EffortLevel.HIGH, fast=True)


def test_read_live_non_priority_tier_is_not_fast(tmp_path: Path) -> None:
    _write_rollout(tmp_path, {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "service_tier": "default"})
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    live = resolver.read_live()
    assert live is not None
    assert live.fast is False


def test_switch_is_unavailable_read_only(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort=EffortLevel.HIGH, fast=True),
        lambda line: sent.append(line) or True,
    )
    assert not result.ok
    assert sent == []
