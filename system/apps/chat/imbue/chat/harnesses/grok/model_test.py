"""Unit tests for grok's model bar: its catalog, the models an account lists, and the switch through the control
lane."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.harnesses.grok.model import BINARY_DEFAULT_MODELS_PATH
from imbue.chat.harnesses.grok.model import GROK_CATALOG
from imbue.chat.harnesses.grok.model import GROK_MODELS_COMMAND
from imbue.chat.harnesses.grok.model import GrokModelResolver
from imbue.chat.harnesses.grok.model import build_catalog_options
from imbue.chat.harnesses.grok.model import is_control_command_in_force
from imbue.chat.harnesses.grok.model import list_grok_model_ids
from imbue.chat.harnesses.grok.model import offered_catalog_options
from imbue.chat.harnesses.grok.model import parse_grok_models_output
from imbue.chat.harnesses.grok.queue_tracker import GrokControlCommand
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
from imbue.chat.harnesses.grok.testing import effort_command
from imbue.chat.harnesses.grok.testing import make_grok_agent_info
from imbue.chat.harnesses.grok.testing import make_grok_agent_state_dir
from imbue.chat.harnesses.grok.testing import model_command
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.model import PickerMode
from imbue.chat.harnesses.model import SwitchMode
from imbue.chat.harnesses.model import match_option
from imbue.chat.harnesses.model import read_model_identity
from imbue.chat.harnesses.model import resolve_model_choice
from imbue.chat.harnesses.model import validate_model_pick
from imbue.chat.harnesses.registry import build_resolver
from imbue.chat.harnesses.registry import get_catalog
from imbue.chat.harnesses.registry import get_model_state_path
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr_grok.resources.grok_statusline import MODEL_STATE_FILENAME

# What `grok models` printed on grok 1.0.62 against mngr's stub model server.
_LISTED_FOR_AN_API_KEY = (
    "You are using XAI_API_KEY.\n\nDefault model: grok-4.6\n\nAvailable models:\n"
    "  * grok-4.6 (default)\n  - grok-build\n  - stub-model\n"
)
_LISTED_FOR_NO_CREDENTIAL = (
    "You are not authenticated.\n\nDefault model: grok-4.6 (not in the list below)\n\nAvailable models:\n"
)
_LISTED_FOR_A_CUSTOM_MODEL = (
    "Model 'stub' is using its own API key.\n\nDefault model: stub\n\nAvailable models:\n  * stub (default)\n"
)
_ACCOUNT_ENVIRONMENT: Mapping[str, str] = {"GROK_HOME": "/accounts/grok-7f3a", "PATH": "/usr/bin:/bin"}


def _binary_models() -> list[dict[str, Any]]:
    return json.loads(BINARY_DEFAULT_MODELS_PATH.read_text())["models"]


def _finished(stdout: str = "", returncode: int | None = 0, is_timed_out: bool = False) -> FinishedProcess:
    return FinishedProcess(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        command=GROK_MODELS_COMMAND,
        is_timed_out=is_timed_out,
        is_output_already_logged=False,
    )


class _ScriptedModelsCommand:
    """`grok models` answering with a fixed result, recording the environment of each run."""

    def __init__(self, finished: FinishedProcess) -> None:
        self.finished = finished
        self.environments: list[Mapping[str, str]] = []

    def __call__(self, environment: Mapping[str, str]) -> FinishedProcess:
        self.environments.append(environment)
        return self.finished


def _unused_send(_line: str) -> bool:
    raise AssertionError("grok's only typist is the flush worker")


def test_the_catalog_is_grok_1_0_62_s_built_in_model_set() -> None:
    assert [(option.id, option.label) for option in GROK_CATALOG.options] == [
        (model["id"], model["name"]) for model in _binary_models()
    ]
    for option, model in zip(GROK_CATALOG.options, _binary_models(), strict=True):
        assert {choice.level for choice in option.efforts} == {
            effort["value"] for effort in model["reasoning_efforts"]
        }
        assert [effort["value"] for effort in model["reasoning_efforts"] if effort.get("default")] == [
            option.default_effort
        ]
        assert option.supports_fast is False
        assert option.in_picker is True
        assert option.harness_reported_model_id is None


def test_the_catalog_offers_each_models_efforts_lowest_first() -> None:
    assert [(option.id, tuple(choice.level for choice in option.efforts)) for option in GROK_CATALOG.options] == [
        ("grok-4.6", ("low", "medium", "high", "xhigh")),
        ("grok-4.5", ("low", "medium", "high")),
    ]


def test_a_model_whose_api_id_differs_reports_that_id_and_one_without_efforts_has_no_effort_axis() -> None:
    (option,) = build_catalog_options({"models": [{"id": "fast-alias", "model": "grok-fast-2", "name": "Grok Fast"}]})

    assert option.id == "fast-alias"
    assert option.harness_reported_model_id == "grok-fast-2"
    assert option.efforts == ()
    assert option.default_effort is None
    assert match_option(ModelIdentity(model_id="grok-fast-2", effort=None, fast=False), (option,)) == option


def test_the_registry_serves_the_catalog_and_switches_eagerly_from_a_list() -> None:
    assert get_catalog(HarnessType.GROK) == GROK_CATALOG
    assert GROK_CATALOG.switch_mode is SwitchMode.EAGER_THEN_RECONCILE
    assert GROK_CATALOG.picker_mode is PickerMode.LIST
    assert GROK_CATALOG.powered_by_text == "Powered by Grok Build"


def test_every_model_matches_a_live_read_at_each_of_its_efforts_and_no_other() -> None:
    for option in GROK_CATALOG.options:
        for choice in option.efforts:
            identity = ModelIdentity(model_id=option.id, effort=choice.level, fast=False)
            assert match_option(identity, GROK_CATALOG.options) == option
    grok_4_5_at_xhigh = ModelIdentity(model_id="grok-4.5", effort="xhigh", fast=False)
    assert match_option(grok_4_5_at_xhigh, GROK_CATALOG.options) is None


def test_a_pick_may_leave_the_effort_unnamed_to_run_at_the_models_default() -> None:
    assert validate_model_pick(GROK_CATALOG.options, "grok-4.5", None, False).id == "grok-4.5"


def test_the_bar_reads_the_model_state_file_mngr_writes_for_grok(tmp_path: Path) -> None:
    model_state_path = get_model_state_path(HarnessType.GROK, tmp_path)
    assert model_state_path == tmp_path / MODEL_STATE_FILENAME
    assert read_model_identity(model_state_path) is None

    model_state_path.write_text(json.dumps({"model": "grok-4.6", "effort": "xhigh", "fast": False}))

    identity = read_model_identity(model_state_path)
    assert identity == ModelIdentity(model_id="grok-4.6", effort="xhigh", fast=False)
    assert resolve_model_choice(identity, GROK_CATALOG.options).matched == GROK_CATALOG.options[0]


def test_a_switch_holds_one_command_per_changed_axis_on_the_agents_control_lane(tmp_path: Path) -> None:
    agent_state_dir = make_grok_agent_state_dir(tmp_path)
    resolver = build_resolver(make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-switch"))

    result = resolver.switch(
        ModelIdentity(model_id="grok-4.5", effort="low", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT, ModelAxis.FAST}),
        _unused_send,
    )

    assert result.ok is True
    queue = get_tracker(agent_state_dir)
    assert queue.snapshot() == []
    first = queue.next_control_command()
    assert first == model_command("grok-4.5")
    queue.settle_control_command(first, is_delivered=True)
    second = queue.next_control_command()
    assert second == effort_command("low")
    queue.settle_control_command(second, is_delivered=True)
    assert queue.has_control_commands() is False


@pytest.mark.parametrize(
    ("identity", "axes"),
    [
        pytest.param(ModelIdentity(model_id="grok-4.6", effort=None, fast=False), {ModelAxis.EFFORT}, id="no_effort"),
        pytest.param(ModelIdentity(model_id="grok-4.6", effort="low", fast=False), {ModelAxis.FAST}, id="fast_only"),
        pytest.param(ModelIdentity(model_id="grok-4.6", effort="low", fast=False), set(), id="no_axis"),
    ],
)
def test_a_switch_holds_nothing_for_an_axis_grok_cannot_change(
    tmp_path: Path, identity: ModelIdentity, axes: set[ModelAxis]
) -> None:
    agent_state_dir = make_grok_agent_state_dir(tmp_path)

    result = GrokModelResolver.build(make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-noop")).switch(
        identity, frozenset(axes), _unused_send
    )

    assert result.ok is True
    assert get_tracker(agent_state_dir).has_control_commands() is False


@pytest.mark.parametrize(
    ("command", "identity", "is_in_force"),
    [
        pytest.param(
            model_command("grok-4.5"),
            ModelIdentity(model_id="grok-4.5", effort="high", fast=False),
            True,
            id="same_model",
        ),
        pytest.param(
            model_command("grok-4.5"),
            ModelIdentity(model_id="grok-4.6", effort="high", fast=False),
            False,
            id="other_model",
        ),
        pytest.param(
            model_command("stub-model-two"),
            ModelIdentity(model_id="stub-model-two", effort=None, fast=False),
            True,
            id="same_model_outside_the_catalog",
        ),
        pytest.param(
            effort_command("high"),
            ModelIdentity(model_id="grok-4.6", effort="high", fast=False),
            True,
            id="same_effort",
        ),
        pytest.param(
            effort_command("low"),
            ModelIdentity(model_id="grok-4.6", effort="high", fast=False),
            False,
            id="other_effort",
        ),
        pytest.param(
            effort_command("low"),
            ModelIdentity(model_id="stub-model-two", effort=None, fast=False),
            False,
            id="a_model_without_efforts",
        ),
        pytest.param(model_command("grok-4.6"), None, False, id="no_model_state_yet"),
    ],
)
def test_a_control_command_is_in_force_when_the_model_state_already_records_it(
    command: GrokControlCommand, identity: ModelIdentity | None, is_in_force: bool
) -> None:
    assert is_control_command_in_force(command, identity) is is_in_force


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param(_LISTED_FOR_AN_API_KEY, ("grok-4.6", "grok-build", "stub-model"), id="api_key"),
        pytest.param(_LISTED_FOR_NO_CREDENTIAL, (), id="no_credential"),
        pytest.param(_LISTED_FOR_A_CUSTOM_MODEL, ("stub",), id="custom_model"),
        pytest.param("", (), id="no_output"),
    ],
)
def test_grok_models_output_parses_to_its_model_ids(output: str, expected: tuple[str, ...]) -> None:
    assert parse_grok_models_output(output) == expected


def test_grok_models_runs_with_the_given_environment_as_its_whole_environment(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_grok = bin_dir / "grok"
    fake_grok.write_text(
        "#!/bin/sh\n"
        '[ "$1" = models ] || exit 2\n'
        'printf "%s|%s" "$GROK_HOME" "${XAI_API_KEY:-unset}" > "$GROK_HOME/seen"\n'
        "printf 'Available models:\\n  * grok-4.5 (default)\\n'\n"
    )
    fake_grok.chmod(0o755)
    grok_home = tmp_path / "account"
    grok_home.mkdir()

    listed = list_grok_model_ids({"PATH": f"{bin_dir}:/usr/bin:/bin", "GROK_HOME": str(grok_home)})

    assert listed == ("grok-4.5",)
    assert (grok_home / "seen").read_text() == f"{grok_home}|unset"


def test_grok_models_that_cannot_start_gives_no_answer(tmp_path: Path) -> None:
    assert list_grok_model_ids({"PATH": str(tmp_path)}) is None


@pytest.mark.parametrize(
    "finished",
    [
        pytest.param(_finished(_LISTED_FOR_AN_API_KEY, returncode=1), id="fails"),
        pytest.param(_finished(returncode=None, is_timed_out=True), id="times_out"),
    ],
)
def test_grok_models_that_fails_gives_no_answer(finished: FinishedProcess) -> None:
    assert list_grok_model_ids(_ACCOUNT_ENVIRONMENT, _ScriptedModelsCommand(finished)) is None


@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        pytest.param(("grok-4.6", "grok-build", "stub-model"), (GROK_CATALOG.options[0],), id="some_catalog_models"),
        pytest.param(("grok-4.5", "grok-4.6"), GROK_CATALOG.options, id="in_catalog_order"),
        pytest.param(("stub",), None, id="no_catalog_model"),
        pytest.param((), None, id="none"),
        pytest.param(None, None, id="no_answer"),
    ],
)
def test_the_offer_is_the_catalog_models_a_listing_names_else_the_whole_catalog(
    listed: tuple[str, ...] | None, expected: tuple[Any, ...] | None
) -> None:
    assert offered_catalog_options(listed) == expected


def test_an_agent_is_offered_the_catalog_models_grok_lists_under_its_accounts_environment(tmp_path: Path) -> None:
    agent_state_dir = make_grok_agent_state_dir(tmp_path)
    models_command = _ScriptedModelsCommand(_finished(_LISTED_FOR_AN_API_KEY))
    resolver = GrokModelResolver.build(
        make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-offer"),
        read_account_environment=lambda: _ACCOUNT_ENVIRONMENT,
        run_models_command=models_command,
    )

    assert resolver.list_offered_models() == ("grok-4.6",)
    assert models_command.environments == [_ACCOUNT_ENVIRONMENT]


def test_an_agent_whose_account_lists_no_catalog_model_is_offered_the_whole_catalog(tmp_path: Path) -> None:
    agent_state_dir = make_grok_agent_state_dir(tmp_path)
    resolver = GrokModelResolver.build(
        make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-offer"),
        read_account_environment=lambda: _ACCOUNT_ENVIRONMENT,
        run_models_command=_ScriptedModelsCommand(_finished(_LISTED_FOR_NO_CREDENTIAL)),
    )

    assert resolver.list_offered_models() is None


def test_an_agent_with_no_account_environment_is_offered_the_whole_catalog_without_running_grok(
    tmp_path: Path,
) -> None:
    agent_state_dir = make_grok_agent_state_dir(tmp_path)
    models_command = _ScriptedModelsCommand(_finished(_LISTED_FOR_AN_API_KEY))
    resolver = GrokModelResolver.build(
        make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-offer"), run_models_command=models_command
    )

    assert resolver.list_offered_models() is None
    assert models_command.environments == []
    assert (
        build_resolver(make_grok_agent_info(agent_state_dir, agent_state_dir.name, "grok-offer")).list_offered_models()
        is None
    )
