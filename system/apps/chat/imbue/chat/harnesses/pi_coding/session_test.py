"""Tests for pi's session: the option set an agent's live model is matched against."""

import json
from pathlib import Path
from typing import NoReturn

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import resolve_model_choice
from imbue.chat.harnesses.model import to_options
from imbue.chat.harnesses.pi_coding.session import PiHarnessSession
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps

_CATALOG = to_options((("groq/llama-3.3-70b-versatile", ("off",)),))


def _unused(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("unused")


def _session(state_dir: Path) -> PiHarnessSession:
    session = PiHarnessSession.build(
        SessionDeps(
            harness=HarnessType.PI_CODING,
            state_dir=state_dir,
            model_state_path=state_dir / "model_state.json",
            send_to_harness=lambda text, is_resent_when_unsure: SendOutcome.OK,
            notify_agents_changed=lambda: None,
            is_harness_starting_up=lambda: False,
            is_tracked=lambda: True,
            on_queue_snapshot=lambda snapshot: None,
            on_user_turn=lambda event: None,
            recompute_activity=lambda: None,
            clear_queue_state=lambda: None,
            catalog_options=lambda: _CATALOG,
            build_interrupter=_unused,
            build_shoulder_tap=lambda agent_info: None,
        )
    )
    assert isinstance(session, PiHarnessSession)
    return session


def test_an_agent_with_no_models_file_matches_against_the_catalog_alone(tmp_path: Path) -> None:
    assert _session(tmp_path).switch_options() == _CATALOG


def test_a_local_model_is_an_option_so_the_model_bar_can_name_it(tmp_path: Path) -> None:
    """An identity that matches no option renders as no model at all, and no catalog lists this one."""
    config_dir = tmp_path / "plugin" / "pi_coding"
    config_dir.mkdir(parents=True)
    (config_dir / "models.json").write_text(
        json.dumps({"providers": {"local-model": {"models": [{"id": "qwen2.5:7b"}]}}})
    )

    options = _session(tmp_path).switch_options()

    assert [option.id for option in options] == ["groq/llama-3.3-70b-versatile", "local-model/qwen2.5:7b"]
    live = ModelIdentity(model_id="local-model/qwen2.5:7b", effort="off", fast=False)
    matched = resolve_model_choice(live, options).matched
    assert matched is not None
    assert matched.id == "local-model/qwen2.5:7b"
