"""Unit tests for antigravity's model catalog and resolver."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.antigravity.model import ANTIGRAVITY_CATALOG
from imbue.chat.harnesses.antigravity.model import AntigravityModelResolver
from imbue.chat.harnesses.antigravity.model import model_command
from imbue.chat.harnesses.antigravity.queue_tracker import OUTBOX_FILENAME
from imbue.chat.harnesses.antigravity.queue_tracker import drop_tracker
from imbue.chat.harnesses.antigravity.queue_tracker import get_tracker
from imbue.chat.harnesses.antigravity.session import AntigravityHarnessSession
from imbue.chat.harnesses.antigravity.testing import build_agent_info
from imbue.chat.harnesses.antigravity.testing import refuse_to_type
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import match_option
from imbue.chat.harnesses.model import validate_model_pick
from imbue.chat.harnesses.session import SessionDeps


@pytest.mark.parametrize(
    ("recorded_model", "recorded_effort", "expected_option_id"),
    [
        # What the statusline records for each listed model, as captured from agy 1.4.3.
        ("Gemini 3.8 Flash", "high", "gemini-3.8-flash"),
        ("Gemini 3.8 Flash", "low", "gemini-3.8-flash"),
        ("Claude Sonnet 4.6 (Thinking)", None, "claude-sonnet-4-6"),
        ("Claude Opus 4.6 (Thinking)", None, "claude-opus-4-6-thinking"),
        ("GPT-OSS 120B (Medium)", None, "gpt-oss-120b-medium"),
    ],
)
def test_each_recorded_model_state_matches_its_catalog_option(
    recorded_model: str, recorded_effort: str | None, expected_option_id: str
) -> None:
    identity = ModelIdentity(model_id=recorded_model, effort=recorded_effort, fast=False)
    matched = match_option(identity, ANTIGRAVITY_CATALOG.options)
    assert matched is not None
    assert matched.id == expected_option_id


@pytest.mark.parametrize(
    ("model_id", "effort", "expected_command"),
    [
        ("gemini-3.8-flash", "low", "/model gemini-3.8-flash-low"),
        ("gemini-3.8-flash", "high", "/model gemini-3.8-flash-high"),
        # A bare base slug starts the model at agy's default effort.
        ("gemini-3.8-flash", None, "/model gemini-3.8-flash"),
        ("claude-sonnet-4-6", None, "/model claude-sonnet-4-6"),
        ("gpt-oss-120b-medium", None, "/model gpt-oss-120b-medium"),
    ],
)
def test_model_command_names_the_model_at_its_effort(model_id: str, effort: str | None, expected_command: str) -> None:
    assert model_command(ModelIdentity(model_id=model_id, effort=effort, fast=False)) == expected_command


def test_every_offered_pick_passes_validation() -> None:
    """Each model at each of its efforts is a pick the model bar's route accepts."""
    options = ANTIGRAVITY_CATALOG.options
    for option in options:
        for effort in option.efforts:
            validate_model_pick(options, option.id, effort.level, fast=False)
        if not option.efforts:
            validate_model_pick(options, option.id, None, fast=False)


def _agent_info(tmp_path: Path) -> AgentInfo:
    agent_info = build_agent_info(tmp_path)
    drop_tracker(agent_info.id)
    return agent_info


def _held_command(agent_info: AgentInfo) -> str | None:
    tracker = get_tracker(agent_info.id, agent_info.agent_state_dir / OUTBOX_FILENAME, "")
    return tracker.model_command()


def test_a_switch_holds_its_command_for_the_worker(tmp_path: Path) -> None:
    agent_info = _agent_info(tmp_path)
    result = AntigravityModelResolver.build(agent_info).switch(
        ModelIdentity(model_id="gemini-3.8-flash", effort="medium", fast=False),
        frozenset({ModelAxis.EFFORT}),
        refuse_to_type,
    )
    assert result.ok
    assert _held_command(agent_info) == "/model gemini-3.8-flash-medium"


def test_a_switch_that_changes_neither_model_nor_effort_holds_nothing(tmp_path: Path) -> None:
    agent_info = _agent_info(tmp_path)
    result = AntigravityModelResolver.build(agent_info).switch(
        ModelIdentity(model_id="claude-sonnet-4-6", effort=None, fast=False), frozenset(), refuse_to_type
    )
    assert result.ok
    assert _held_command(agent_info) is None


def _session(model_state_path: Path) -> AntigravityHarnessSession:
    unused: Callable[..., object] = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unused"))
    return AntigravityHarnessSession.build(
        SessionDeps(
            harness=HarnessType.ANTIGRAVITY,
            state_dir=model_state_path.parent,
            model_state_path=model_state_path,
            send_to_harness=lambda text: True,
            notify_agents_changed=lambda: None,
            is_harness_starting_up=lambda: False,
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


def test_a_known_model_does_not_add_a_derived_option(tmp_path: Path) -> None:
    state = tmp_path / "model_state.json"
    state.write_text(json.dumps({"model": "Gemini 3.8 Flash", "effort": "medium"}))
    assert _session(state).switch_options() == ANTIGRAVITY_CATALOG.options


@pytest.mark.parametrize(
    ("recorded_state", "expected_efforts"),
    [
        # A model newer than the list, at an adjustable effort: its one effort is declared so the
        # live read matches, and no effort control is offered for levels nobody listed.
        ({"model": "Gemini 9.9 Flash", "effort": "high"}, ("high",)),
        ({"model": "Gemini 4 Argon (Thinking)"}, ()),
    ],
)
def test_an_unknown_model_is_rendered_instead_of_shrugged(
    tmp_path: Path, recorded_state: dict[str, str], expected_efforts: tuple[str, ...]
) -> None:
    """When Google ships a model newer than the list -- worst case, a new default -- every agy
    agent would otherwise show the shrug, and the shrug offers no picker to switch away with."""
    state = tmp_path / "model_state.json"
    state.write_text(json.dumps(recorded_state))
    identity = ModelIdentity(model_id=recorded_state["model"], effort=recorded_state.get("effort"), fast=False)
    assert match_option(identity, ANTIGRAVITY_CATALOG.options) is None

    options = _session(state).switch_options()
    matched = match_option(identity, options)

    assert matched is not None
    assert matched.label == recorded_state["model"]
    assert tuple(effort.level for effort in matched.efforts) == expected_efforts
    assert matched.in_picker is False
    assert options[: len(ANTIGRAVITY_CATALOG.options)] == ANTIGRAVITY_CATALOG.options


def test_a_carried_pick_of_an_unlisted_model_types_nothing(tmp_path: Path) -> None:
    """A rebind carries the chat's model, which can be one newer than the list. The restarted agent
    still reports it, so the pick validates against its derived option, but its id is a display
    name `/model` does not take, and the agent is already on it."""
    state = tmp_path / "model_state.json"
    state.write_text(json.dumps({"model": "Gemini 9.9 Flash", "effort": "high"}))
    agent_info = _agent_info(tmp_path)
    validate_model_pick(_session(state).switch_options(), "Gemini 9.9 Flash", "high", fast=False)

    result = AntigravityModelResolver.build(agent_info).switch(
        ModelIdentity(model_id="Gemini 9.9 Flash", effort="high", fast=False), frozenset(ModelAxis), refuse_to_type
    )

    assert result.ok
    assert _held_command(agent_info) is None


def test_no_model_state_leaves_the_catalog_alone(tmp_path: Path) -> None:
    """Before agy's first statusline fire there is no file, so the bar renders no slots."""
    assert _session(tmp_path / "absent.json").switch_options() == ANTIGRAVITY_CATALOG.options
