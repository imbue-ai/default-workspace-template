"""Unit tests for pi's parsed catalog and its model resolver."""

import json
from pathlib import Path
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.claude.model import ClaudeModelResolver
from imbue.system_interface.harnesses.pi_coding.model import PiModelResolver
from imbue.system_interface.harnesses.pi_coding.model import _parse_list_models
from imbue.system_interface.harnesses.pi_coding.model import _supported_thinking_levels
from imbue.system_interface.harnesses.pi_coding.model import build_catalog


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="pi-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.PI_CODING,
    )


# --- effort derivation (pi's getSupportedThinkingLevels) ------------------------


def test_non_reasoning_model_supports_only_off() -> None:
    # The reasoning gate short-circuits: a non-reasoning model offers only "off",
    # whatever its thinkingLevelMap says.
    assert _supported_thinking_levels({"reasoning": False, "thinkingLevelMap": {"high": "high"}}) == ("off",)


def test_reasoning_model_sparse_map_nulls_disable_and_xhigh_max_opt_in() -> None:
    # A nulled level is dropped; xhigh/max appear only when explicitly mapped; every
    # other level is on. (This mirrors a real anthropic entry: off nulled, xhigh/max mapped.)
    levels = _supported_thinking_levels(
        {"reasoning": True, "thinkingLevelMap": {"off": None, "xhigh": "xhigh", "max": "max"}}
    )
    assert levels == ("minimal", "low", "medium", "high", "xhigh", "max")


def test_reasoning_model_no_map_drops_xhigh_and_max() -> None:
    # With no map, everything up to high is on, but xhigh/max are opt-in only.
    assert _supported_thinking_levels({"reasoning": True}) == ("off", "minimal", "low", "medium", "high")


# --- catalog build (efforts are verbatim strings) -------------------------------


def _write_provider(data_dir: Path, provider: str, models: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / f"{provider}.json").write_text(json.dumps({f"{provider}-api": models}))


def test_build_catalog_tags_and_string_efforts(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_provider(
        data_dir,
        "anthropic",
        {
            "claude-sonnet-5": {"reasoning": True, "thinkingLevelMap": {"off": None, "xhigh": "xhigh"}},
            "haiku-lite": {"reasoning": False},
        },
    )
    catalog = build_catalog(data_dir)
    assert catalog.picker_mode == PickerMode.SEARCH
    assert catalog.switch_mode == SwitchMode.ON_CHANGE
    # No launch default -- pi is many-auth.
    assert catalog.default_model_id == ""
    by_id = {opt.id: opt for opt in catalog.options}
    # id == label == provider/model
    assert by_id["anthropic/claude-sonnet-5"].label == "anthropic/claude-sonnet-5"
    # efforts are the model's thinking levels, verbatim strings, in pi's order
    assert [e.level for e in by_id["anthropic/claude-sonnet-5"].efforts] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    # a non-reasoning model still declares its single "off" level
    assert [e.level for e in by_id["anthropic/haiku-lite"].efforts] == ["off"]
    # no model supports fast
    assert all(not opt.supports_fast for opt in catalog.options)


# --- pi --list-models parsing (the authed offer set) ----------------------------


def test_parse_list_models_takes_provider_and_model_columns() -> None:
    # Real `pi --list-models` output: a header row then whitespace-column rows whose
    # first two columns are provider and model.
    output = (
        "provider   model                     context  max-out  thinking  images\n"
        "anthropic  claude-opus-4-8           1M       128K     yes       yes\n"
        "anthropic  claude-sonnet-5           1M       128K     yes       yes\n"
    )
    assert _parse_list_models(output) == ("anthropic/claude-opus-4-8", "anthropic/claude-sonnet-5")


def test_parse_list_models_no_header_is_empty() -> None:
    # When unauthenticated pi prints a message with no table header -> no offer set.
    assert _parse_list_models("No models available. Use /login to authenticate.\n") == ()


def test_parse_list_models_empty_output_is_empty() -> None:
    assert _parse_list_models("") == ()


# --- resolver -------------------------------------------------------------------


def test_list_offered_models_defaults_to_whole_catalog_for_non_pi(tmp_path: Path) -> None:
    # The base resolver hook returns None (offer the whole catalog); only a dynamic,
    # account-gated harness overrides it.
    assert ClaudeModelResolver.build(_agent_info(tmp_path)).list_offered_models() is None


def test_guess_from_launch_is_none(tmp_path: Path) -> None:
    # pi has no knowable launch default -> logo-only until the live model is recorded.
    assert PiModelResolver.build(_agent_info(tmp_path)).guess_from_launch() is None


def test_read_live_is_none_before_the_state_file_exists(tmp_path: Path) -> None:
    assert PiModelResolver.build(_agent_info(tmp_path)).read_live() is None


def test_read_live_reads_the_state_file(tmp_path: Path) -> None:
    (tmp_path / "pi_model_state.json").write_text(
        json.dumps({"provider": "anthropic", "model": "claude-opus-4-8", "thinking_level": "high"})
    )
    live = PiModelResolver.build(_agent_info(tmp_path)).read_live()
    assert live == ModelIdentity(model_id="anthropic/claude-opus-4-8", effort="high", fast=False)


def test_read_live_off_is_a_real_effort_not_dropped(tmp_path: Path) -> None:
    (tmp_path / "pi_model_state.json").write_text(
        json.dumps({"provider": "google", "model": "gemini-flash", "thinking_level": "off"})
    )
    live = PiModelResolver.build(_agent_info(tmp_path)).read_live()
    assert live is not None
    assert live.effort == "off"


def test_switch_writes_the_control_file(tmp_path: Path) -> None:
    resolver = PiModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="anthropic/claude-opus-4-8", effort="high", fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda line: True,
    )
    assert result.ok
    control = (tmp_path / "pi_control.jsonl").read_text().splitlines()
    assert json.loads(control[0]) == {"model_id": "anthropic/claude-opus-4-8", "thinking_level": "high"}
