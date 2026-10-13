"""Unit tests for claude's session: which model set its chip and switch check against."""

import json
from pathlib import Path
from typing import Any

from imbue.chat.harnesses.claude.gateway_models import GATEWAY_MODELS_FILE_NAME
from imbue.chat.harnesses.claude.model import CLAUDE_CATALOG
from imbue.chat.harnesses.claude.model import CLAUDE_EFFORTS
from imbue.chat.harnesses.claude.session import ClaudeHarnessSession
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelOption
from imbue.chat.harnesses.registry import get_harness_spec
from imbue.chat.harnesses.session import SessionDeps

_CONCENTRATE_MODELS = Path(__file__).parent / "testdata" / "concentrate_models.json"


def _session(state_dir: Path, account_dir: Path) -> ClaudeHarnessSession:
    """A session for an agent whose env pins its claude config dir (its account) to ``account_dir``."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "env").write_text(f"CLAUDE_CONFIG_DIR={account_dir}\n")
    unused: Any = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unused"))
    return ClaudeHarnessSession.build(
        SessionDeps(
            harness=HarnessType.CLAUDE,
            state_dir=state_dir,
            model_state_path=state_dir / "model_state.json",
            send_to_harness=unused,
            notify_agents_changed=lambda: None,
            is_harness_starting_up=lambda: False,
            is_tracked=lambda: True,
            on_queue_snapshot=lambda snapshot: None,
            on_user_turn=lambda event: None,
            recompute_activity=lambda: None,
            clear_queue_state=lambda: None,
            catalog_options=lambda: CLAUDE_CATALOG.options,
            build_interrupter=unused,
            build_shoulder_tap=lambda agent_info: None,
        )
    )


def _ids(options: tuple[ModelOption, ...]) -> list[str]:
    return [option.id for option in options]


def test_claude_agents_get_the_claude_session() -> None:
    assert get_harness_spec(HarnessType.CLAUDE).session_class is ClaudeHarnessSession


def test_an_agent_on_anthropic_checks_against_the_catalog(tmp_path: Path) -> None:
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    session = _session(tmp_path / "agent", account_dir)
    assert session.switch_options() == CLAUDE_CATALOG.options


def test_after_a_restart_the_list_kept_for_the_account_names_a_gateway_model(tmp_path: Path) -> None:
    """Before anyone has opened the picker again, the chip still knows a model only the gateway has."""
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    (account_dir / GATEWAY_MODELS_FILE_NAME).write_text(_CONCENTRATE_MODELS.read_text())
    session = _session(tmp_path / "agent", account_dir)
    options = session.switch_options()
    assert "kimi-k2-6" in _ids(options)
    assert _ids(options)[: len(CLAUDE_CATALOG.options)] == _ids(CLAUDE_CATALOG.options)


def test_a_picker_open_replaces_the_set_the_session_checks_against(tmp_path: Path) -> None:
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    (account_dir / GATEWAY_MODELS_FILE_NAME).write_text(_CONCENTRATE_MODELS.read_text())
    session = _session(tmp_path / "agent", account_dir)
    assert "kimi-k2-6" in _ids(session.switch_options())
    glm = ModelOption(id="glm-5", label="GLM 5", efforts=CLAUDE_EFFORTS, supports_fast=False)
    offered = (*CLAUDE_CATALOG.options, glm)
    session.note_offered_options(offered)
    assert session.switch_options() == offered


def test_the_kept_list_is_read_once_and_then_held(tmp_path: Path) -> None:
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    kept_path = account_dir / GATEWAY_MODELS_FILE_NAME
    kept_path.write_text(_CONCENTRATE_MODELS.read_text())
    session = _session(tmp_path / "agent", account_dir)
    first = session.switch_options()
    kept_path.write_text(json.dumps({"data": [{"id": "glm-5"}]}))
    assert session.switch_options() == first


def test_an_empty_offer_does_not_clear_to_nothing(tmp_path: Path) -> None:
    """A note of nothing (no caller sends one, but the set must never go empty) reads as no note."""
    account_dir = tmp_path / "account"
    account_dir.mkdir()
    session = _session(tmp_path / "agent", account_dir)
    session.note_offered_options(())
    assert session.switch_options() == CLAUDE_CATALOG.options
