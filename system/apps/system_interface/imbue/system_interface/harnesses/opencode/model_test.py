"""Unit tests for opencode's parsed catalog and its model resolver."""

import json
from pathlib import Path
from typing import Any

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.model import ModelAxis
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import PickerMode
from imbue.system_interface.harnesses.model import SwitchMode
from imbue.system_interface.harnesses.opencode.model import OpenCodeModelResolver
from imbue.system_interface.harnesses.opencode.model import _efforts
from imbue.system_interface.harnesses.opencode.model import _parse_models_output
from imbue.system_interface.harnesses.opencode.model import build_catalog


def _agent_info(tmp_path: Path) -> AgentInfo:
    return AgentInfo(
        id="opencode-1",
        name="a",
        state="RUNNING",
        agent_state_dir=tmp_path,
        claude_config_dir=tmp_path / "unused",
        harness=HarnessType.OPENCODE,
    )


# --- effort derivation (opencode's variant synthesis) ---------------------------


def test_effort_type_values_are_ordered_by_the_ladder() -> None:
    # models.dev may list values in any order; the catalog emits them ascending.
    assert _efforts({"reasoning_options": [{"type": "effort", "values": ["high", "low", "medium"]}]}) == (
        "low",
        "medium",
        "high",
    )


def test_budget_tokens_synthesizes_high_and_max() -> None:
    # All Anthropic Claude models are budget_tokens with no values; opencode invents high/max.
    assert _efforts({"reasoning_options": [{"type": "budget_tokens", "min": 1024}]}) == ("high", "max")


def test_toggle_and_missing_have_no_effort_axis() -> None:
    assert _efforts({"reasoning_options": [{"type": "toggle"}]}) == ()
    assert _efforts({}) == ()


def test_none_kept_and_json_null_dropped() -> None:
    # "none" is a real variant (e.g. gpt-5.1) and is kept; a JSON null value is dropped.
    assert _efforts({"reasoning_options": [{"type": "effort", "values": ["none", None, "high"]}]}) == ("none", "high")


# --- catalog build (efforts are verbatim strings) -------------------------------


def _write_cache(cache_path: Path, providers: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(providers))


def test_build_catalog_tags_efforts_and_modes(tmp_path: Path) -> None:
    cache_path = tmp_path / "models.json"
    _write_cache(
        cache_path,
        {
            "anthropic": {"models": {"claude-sonnet-4-5": {"reasoning_options": [{"type": "budget_tokens"}]}}},
            "openai": {"models": {"gpt-5": {"reasoning_options": [{"type": "effort", "values": ["low", "high"]}]}}},
        },
    )
    catalog = build_catalog(cache_path)
    assert catalog.picker_mode == PickerMode.SEARCH
    assert catalog.switch_mode == SwitchMode.ON_CHANGE
    # No launch default -- opencode is many-auth.
    assert catalog.default_model_id == ""
    by_id = {opt.id: opt for opt in catalog.options}
    # id == label == provider/model
    assert by_id["anthropic/claude-sonnet-4-5"].label == "anthropic/claude-sonnet-4-5"
    assert [e.level for e in by_id["anthropic/claude-sonnet-4-5"].efforts] == ["high", "max"]
    assert [e.level for e in by_id["openai/gpt-5"].efforts] == ["low", "high"]
    # no model supports fast
    assert all(not opt.supports_fast for opt in catalog.options)


# --- opencode models parsing (the authed offer set) -----------------------------


def test_parse_models_output_keeps_provider_model_lines() -> None:
    output = "opencode/big-pickle\nanthropic/claude-opus-4-8\n\ngarbage-no-slash\n"
    assert _parse_models_output(output) == ("opencode/big-pickle", "anthropic/claude-opus-4-8")


# --- resolver -------------------------------------------------------------------


def test_read_live_is_none_before_the_state_file_exists(tmp_path: Path) -> None:
    assert OpenCodeModelResolver.build(_agent_info(tmp_path)).read_live() is None


def test_read_live_maps_variant_to_effort(tmp_path: Path) -> None:
    (tmp_path / "opencode_model_state.json").write_text(
        json.dumps({"provider": "anthropic", "model": "claude-opus-4-8", "variant": "max"})
    )
    live = OpenCodeModelResolver.build(_agent_info(tmp_path)).read_live()
    assert live == ModelIdentity(model_id="anthropic/claude-opus-4-8", effort="max", fast=False)


def test_read_live_base_variants_are_no_effort(tmp_path: Path) -> None:
    for base in ("", "default"):
        (tmp_path / "opencode_model_state.json").write_text(
            json.dumps({"provider": "openai", "model": "gpt-4o", "variant": base})
        )
        live = OpenCodeModelResolver.build(_agent_info(tmp_path)).read_live()
        assert live is not None
        assert live.effort is None


def test_guess_from_launch_is_none_without_a_running_server(tmp_path: Path) -> None:
    # No opencode_server_port marker -> the probe cannot reach a server -> None (logo-only).
    assert OpenCodeModelResolver.build(_agent_info(tmp_path)).guess_from_launch() is None


def test_switch_fails_when_the_server_is_not_running(tmp_path: Path) -> None:
    resolver = OpenCodeModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="anthropic/claude-opus-4-8", effort="max", fast=False),
        frozenset({ModelAxis.MODEL}),
        lambda line: True,
    )
    assert not result.ok


def test_switch_is_a_noop_when_no_model_or_effort_axis_changed(tmp_path: Path) -> None:
    resolver = OpenCodeModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="anthropic/claude-opus-4-8", effort="max", fast=True),
        frozenset({ModelAxis.FAST}),
        lambda line: True,
    )
    assert result.ok
