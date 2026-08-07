"""Unit tests for the Codex model resolver."""

import json
from pathlib import Path

from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.model import CODEX_CATALOG
from imbue.system_interface.harnesses.codex.model import CodexModelResolver
from imbue.system_interface.harnesses.codex.model import codex_model_state_path
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import SwitchMode


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.CODEX,
    )


def _write_model_state(tmp_path: Path, state: dict[str, object]) -> None:
    """Write the minds_model_state.json mirror the patched codex maintains."""
    path = codex_model_state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def test_catalog_is_eager_then_reconcile() -> None:
    assert CODEX_CATALOG.switch_mode == SwitchMode.EAGER_THEN_RECONCILE


def test_guess_from_launch_defaults_when_no_config(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.guess_from_launch() == ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=False)


def test_guess_from_launch_reads_config_toml(tmp_path: Path) -> None:
    config_path = get_codex_config_path(get_codex_home(tmp_path))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text('model = "gpt-5.5"\nmodel_reasoning_effort = "low"\n')
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.guess_from_launch() == ModelIdentity(model_id="gpt-5.5", effort="low", fast=False)


def test_read_live_is_none_when_state_file_absent(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.read_live() is None


def test_read_live_is_none_when_model_missing(tmp_path: Path) -> None:
    # File present but no model recorded yet -> None (fall back to the guess).
    _write_model_state(tmp_path, {"reasoning_effort": "high"})
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.read_live() is None


def test_read_live_parses_model_state(tmp_path: Path) -> None:
    _write_model_state(tmp_path, {"model": "gpt-5.6-sol", "reasoning_effort": "high", "service_tier": "priority"})
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    # priority service tier reads as fast on.
    assert resolver.read_live() == ModelIdentity(model_id="gpt-5.6-sol", effort="high", fast=True)


def test_read_live_non_priority_tier_is_not_fast(tmp_path: Path) -> None:
    _write_model_state(tmp_path, {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "service_tier": "default"})
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    live = resolver.read_live()
    assert live is not None
    assert live.fast is False


def test_read_live_reflects_a_rewrite(tmp_path: Path) -> None:
    # The mirror is rewritten whole on each change; a later read sees the new value.
    info = _agent_info(tmp_path)
    resolver = CodexModelResolver.build(info)
    _write_model_state(tmp_path, {"model": "gpt-5.6-terra", "reasoning_effort": "medium", "service_tier": "default"})
    assert resolver.read_live() == ModelIdentity(model_id="gpt-5.6-terra", effort="medium", fast=False)
    _write_model_state(tmp_path, {"model": "gpt-5.5", "reasoning_effort": "high", "service_tier": "priority"})
    assert resolver.read_live() == ModelIdentity(model_id="gpt-5.5", effort="high", fast=True)


def test_watched_paths_is_the_model_state_file(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    assert resolver.watched_paths() == (codex_model_state_path(tmp_path),)


def test_switch_model_and_effort_send_one_model_command(tmp_path: Path) -> None:
    # Codex applies model + effort together, so a model change sends one
    # `/model <model> <effort>` -- not a separate /effort.
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-terra", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/model gpt-5.6-terra high"]


def test_switch_effort_only_still_goes_through_model(tmp_path: Path) -> None:
    # Effort has no standalone codex command; it rides /model with the current model.
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="xhigh", fast=False),
        frozenset({ModelAxis.EFFORT}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/model gpt-5.6-sol xhigh"]


def test_switch_fast_toggle_sends_only_fast(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=True),
        frozenset({ModelAxis.FAST}),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == ["/fast on"]


def test_switch_with_no_axes_sends_nothing(tmp_path: Path) -> None:
    resolver = CodexModelResolver.build(_agent_info(tmp_path))
    sent: list[str] = []
    result = resolver.switch(
        ModelIdentity(model_id="gpt-5.6-sol", effort="medium", fast=False),
        frozenset(),
        lambda line: sent.append(line) or True,
    )
    assert result.ok
    assert sent == []
