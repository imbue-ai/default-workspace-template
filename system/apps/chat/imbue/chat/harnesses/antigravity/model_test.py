"""Unit tests for Antigravity's settings-backed model and effort controls."""

import json
from pathlib import Path

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.antigravity.model import ANTIGRAVITY_CATALOG
from imbue.chat.harnesses.antigravity.model import ANTIGRAVITY_HOME_RELATIVE_PATH
from imbue.chat.harnesses.antigravity.model import AntigravityModelResolver
from imbue.chat.harnesses.antigravity.model import derived_option
from imbue.chat.harnesses.antigravity.session import AntigravityHarnessSession
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import match_option
from imbue.chat.harnesses.session import InterruptToComposer
from imbue.chat.harnesses.session import SessionDeps
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path


def _agent_info(tmp_path: Path) -> AgentInfo:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return AgentInfo(
        id="agent-1",
        name="a",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=tmp_path / "claude",
    )


def test_catalog_groups_gemini_tiers_into_effort_controls() -> None:
    flash = next(option for option in ANTIGRAVITY_CATALOG.options if option.id == "gemini-3.7-flash")
    assert flash.label == "Gemini 3.7 Flash"
    assert [choice.level for choice in flash.efforts] == ["low", "medium", "high"]
    assert flash.in_picker is True
    assert ANTIGRAVITY_CATALOG.switch_mode == "eager_then_reconcile"


def test_the_harness_credit_is_declared_whole() -> None:
    assert ANTIGRAVITY_CATALOG.powered_by_text == "Powered by Antigravity"


def test_every_catalog_id_matches_itself() -> None:
    for option in ANTIGRAVITY_CATALOG.options:
        matched = match_option(ModelIdentity(model_id=option.id, effort=None, fast=False), ANTIGRAVITY_CATALOG.options)
        assert matched is not None, option.id


def test_legacy_tier_baked_ids_remain_matchable_without_picker_duplicates() -> None:
    legacy = next(option for option in ANTIGRAVITY_CATALOG.options if option.id == "gemini-3.7-flash-high")
    assert legacy.in_picker is False
    assert (
        match_option(
            ModelIdentity(model_id="gemini-3.7-flash-high", effort=None, fast=False), ANTIGRAVITY_CATALOG.options
        )
        is legacy
    )


def test_derived_option_reconstructs_slug_and_preserves_display_name_fallback() -> None:
    assert derived_option("gemini-3.8-flash-high").label == "Gemini 3.8 Flash (High)"
    assert derived_option("gemini-4.0-pro-low").label == "Gemini 4.0 Pro (Low)"
    assert derived_option("Gemini 4.2 Ultra (High)").label == "Gemini 4.2 Ultra"
    assert derived_option("Gemini 4.2 Ultra (High)").efforts[0].level == "high"
    assert derived_option("claude-sonnet-5").label == "Claude Sonnet 5"
    assert derived_option("gemini-3.8-flash-high").in_picker is False


def _session(model_state_path: Path) -> AntigravityHarnessSession:
    def unused(_agent_info: AgentInfo) -> InterruptToComposer:
        raise AssertionError("unused")

    return AntigravityHarnessSession.build(
        SessionDeps(
            harness=HarnessType.ANTIGRAVITY,
            state_dir=model_state_path.parent,
            model_state_path=model_state_path,
            send_to_harness=lambda text: True,
            notify_agents_changed=lambda: None,
            is_tracked=lambda: True,
            on_queue_snapshot=lambda snapshot: None,
            on_user_turn=lambda event: None,
            recompute_activity=lambda: None,
            clear_queue_state=lambda: None,
            catalog_options=lambda: ANTIGRAVITY_CATALOG.options,
            build_interrupter=unused,
            build_shoulder_tap=lambda agent_info: None,
        )
    )


def test_unknown_model_gets_rendering_fallback(tmp_path: Path) -> None:
    state = tmp_path / "model_state.json"
    state.write_text(json.dumps({"model": "gemini-9.9-flash-high"}))
    options = _session(state).switch_options()
    assert len(options) == len(ANTIGRAVITY_CATALOG.options) + 1
    matched = match_option(ModelIdentity(model_id="gemini-9.9-flash-high", effort=None, fast=False), options)
    assert matched is not None
    assert matched.label == "Gemini 9.9 Flash (High)"


def test_known_model_does_not_add_a_derived_option(tmp_path: Path) -> None:
    state = tmp_path / "model_state.json"
    state.write_text(json.dumps({"model": "Gemini 3.7 Flash (High)"}))
    assert _session(state).switch_options() == ANTIGRAVITY_CATALOG.options


def test_no_model_state_leaves_catalog_alone(tmp_path: Path) -> None:
    assert _session(tmp_path / "absent.json").switch_options() == ANTIGRAVITY_CATALOG.options


def test_display_name_matches_family_and_effort() -> None:
    identity = ModelIdentity(model_id="Gemini 3.7 Flash (High)", effort="high", fast=False)
    matched = match_option(identity, ANTIGRAVITY_CATALOG.options)
    assert matched is not None
    assert matched.id == "gemini-3.7-flash"


def test_switch_writes_agy_display_name_and_preserves_settings(tmp_path: Path) -> None:
    agent_info = _agent_info(tmp_path)
    settings_path = get_antigravity_settings_path(agent_info.agent_state_dir / ANTIGRAVITY_HOME_RELATIVE_PATH)
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"colorScheme": "dark", "trustedWorkspaces": ["/work"]}))
    resolver = AntigravityModelResolver.build(agent_info)

    result = resolver.switch(
        ModelIdentity(model_id="gemini-3.7-flash", effort="high", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
        lambda _text: False,
    )

    assert result.ok is True
    assert json.loads(settings_path.read_text()) == {
        "colorScheme": "dark",
        "trustedWorkspaces": ["/work"],
        "model": "Gemini 3.7 Flash (High)",
    }


def test_switch_rejects_fast_mode(tmp_path: Path) -> None:
    resolver = AntigravityModelResolver.build(_agent_info(tmp_path))
    result = resolver.switch(
        ModelIdentity(model_id="gemini-3.7-flash", effort="high", fast=True),
        frozenset({ModelAxis.FAST}),
        lambda _text: False,
    )
    assert result.ok is False
