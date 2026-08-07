"""Unit tests for antigravity's catalog and its READ-ONLY model resolver."""

import json
from pathlib import Path
from typing import Any

from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.antigravity.model import ANTIGRAVITY_CATALOG
from imbue.system_interface.harnesses.antigravity.model import AntigravityModelResolver


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="agy-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.ANTIGRAVITY,
    )


def _settings_path(tmp_path: Path) -> Path:
    """Where the resolver reads agy's model from (per-agent antigravity-cli/settings.json)."""
    return get_antigravity_settings_path(tmp_path.joinpath("plugin", "antigravity", "home"))


def _write_settings(tmp_path: Path, body: dict[str, Any]) -> None:
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


# --- catalog --------------------------------------------------------------------


def test_catalog_is_a_flat_read_only_list_of_every_model() -> None:
    assert ANTIGRAVITY_CATALOG.switch_mode == SwitchMode.READ_ONLY
    assert ANTIGRAVITY_CATALOG.picker_mode == PickerMode.LIST
    assert ANTIGRAVITY_CATALOG.default_model_id == "Gemini 3.6 Flash (High)"
    labels = [opt.label for opt in ANTIGRAVITY_CATALOG.options]
    assert "Gemini 3.1 Pro (Low)" in labels and "Claude Sonnet 4.6 (Thinking)" in labels
    assert len(ANTIGRAVITY_CATALOG.options) == 11
    # No effort axis (agy enumerates each model+effort as its own entry) and no fast tier.
    assert all(opt.efforts == () and not opt.supports_fast for opt in ANTIGRAVITY_CATALOG.options)
    # id == label so a live read of the display string matches an option with no translation.
    assert all(opt.id == opt.label for opt in ANTIGRAVITY_CATALOG.options)


# --- resolver -------------------------------------------------------------------


def test_read_live_and_guess_are_none_before_settings_exists(tmp_path: Path) -> None:
    resolver = AntigravityModelResolver.build(_agent_info(tmp_path))
    assert resolver.read_live() is None
    assert resolver.guess_from_launch() is None


def test_read_live_reads_the_settings_model_key(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"model": "Gemini 3.1 Pro (Low)", "trustedWorkspaces": []})
    live = AntigravityModelResolver.build(_agent_info(tmp_path)).read_live()
    assert live == ModelIdentity(model_id="Gemini 3.1 Pro (Low)", effort=None, fast=False)


def test_guess_from_launch_reads_the_pinned_model(tmp_path: Path) -> None:
    # The launcher pins settings_overrides.model, so provision writes it into settings.json.
    _write_settings(tmp_path, {"model": "Gemini 3.6 Flash (High)"})
    guess = AntigravityModelResolver.build(_agent_info(tmp_path)).guess_from_launch()
    assert guess == ModelIdentity(model_id="Gemini 3.6 Flash (High)", effort=None, fast=False)


def test_read_live_is_none_when_model_key_absent_or_blank(tmp_path: Path) -> None:
    _write_settings(tmp_path, {"trustedWorkspaces": []})
    assert AntigravityModelResolver.build(_agent_info(tmp_path)).read_live() is None
    _write_settings(tmp_path, {"model": ""})
    assert AntigravityModelResolver.build(_agent_info(tmp_path)).read_live() is None


def test_read_live_is_none_on_unreadable_settings(tmp_path: Path) -> None:
    path = _settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    assert AntigravityModelResolver.build(_agent_info(tmp_path)).read_live() is None


def test_watched_paths_is_the_settings_file(tmp_path: Path) -> None:
    resolver = AntigravityModelResolver.build(_agent_info(tmp_path))
    assert resolver.watched_paths() == (_settings_path(tmp_path),)


def test_switch_is_read_only(tmp_path: Path) -> None:
    resolver = AntigravityModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="Gemini 3.1 Pro (Low)", effort=None, fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda line: True,
    )
    assert not result.ok
    assert result.detail


def test_list_offered_models_defaults_to_the_whole_catalog(tmp_path: Path) -> None:
    assert AntigravityModelResolver.build(_agent_info(tmp_path)).list_offered_models() is None
