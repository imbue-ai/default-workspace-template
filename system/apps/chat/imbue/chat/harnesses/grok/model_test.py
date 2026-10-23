"""Unit tests for grok's model bar: its catalog, its account listing and its switch through the control lane."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.grok.model import GROK_CATALOG
from imbue.chat.harnesses.grok.model import GrokModelResolver
from imbue.chat.harnesses.grok.model import build_grok_models_environment
from imbue.chat.harnesses.grok.model import list_account_model_ids
from imbue.chat.harnesses.grok.model import parse_grok_models_output
from imbue.chat.harnesses.grok.model import read_agent_account_dir
from imbue.chat.harnesses.grok.queue_tracker import drop_tracker
from imbue.chat.harnesses.grok.queue_tracker import get_tracker
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
from imbue.concurrency_group.errors import ProcessError
from imbue.mngr_grok.api import get_model_state_path as get_grok_model_state_path

# `default_models.json` as the pinned grok 1.0.62 binary embeds it, read out of the binary verbatim.
_BINARY_DEFAULT_MODELS = Path(__file__).parent / "binary_default_models_v1_0_62.json"
_BINARY_VERSION = "1.0.62"
_INSTALLER = Path(__file__).parents[6] / "scripts" / "install_grok.sh"

# What `grok models` printed on the pinned 1.0.62 binary against mngr's stub model server.
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


class _Finished:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", is_timed_out: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.is_timed_out = is_timed_out


def _runner_returning(finished: _Finished, seen: dict[str, Any] | None = None):
    def run(**kwargs: Any) -> _Finished:
        if seen is not None:
            seen.update(kwargs)
        return finished

    return run


def _no_runner(**_kwargs: Any) -> _Finished:
    raise AssertionError("grok models must not run")


def _agent_info(state_dir: Path) -> AgentInfo:
    return AgentInfo(
        id=state_dir.name,
        name="grok-chat",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=state_dir / "claude",
        harness=HarnessType.GROK,
    )


def _write_api_key_account(account_dir: Path, key: str = "xai-key-5c1e") -> Path:
    account_dir.mkdir(parents=True, exist_ok=True)
    scope = {"key": key, "auth_mode": "api_key", "create_time": "2026-10-19T19:11:22.318953Z", "user_id": ""}
    (account_dir / "auth.json").write_text(json.dumps({"xai::api_key": scope}))
    return account_dir


def _binary_models() -> list[dict[str, Any]]:
    return json.loads(_BINARY_DEFAULT_MODELS.read_text())["models"]


def test_the_catalog_is_the_pinned_grok_built_in_model_set() -> None:
    assert [(option.id, option.label) for option in GROK_CATALOG.options] == [
        (model["id"], model["name"]) for model in _binary_models()
    ]
    for option, model in zip(GROK_CATALOG.options, _binary_models(), strict=True):
        assert {choice.level for choice in option.efforts} == {
            effort["value"] for effort in model["reasoning_efforts"]
        }
        assert option.default_effort == model["reasoning_effort"]
        assert [effort["value"] for effort in model["reasoning_efforts"] if effort.get("default")] == [
            option.default_effort
        ]
        assert option.supports_fast is False
        assert option.in_picker is True


def test_the_efforts_are_offered_lowest_first() -> None:
    assert [tuple(choice.level for choice in option.efforts) for option in GROK_CATALOG.options] == [
        ("low", "medium", "high", "xhigh"),
        ("low", "medium", "high"),
    ]


def test_the_catalog_fixture_is_read_from_the_grok_the_image_installs() -> None:
    match = re.search(r'^GROK_VERSION="([^"]+)"$', _INSTALLER.read_text(), re.MULTILINE)
    assert match is not None and match.group(1) == _BINARY_VERSION, "re-read default_models.json from the new grok"


def test_the_bar_switches_eagerly_from_a_list_and_credits_grok_build() -> None:
    assert get_catalog(HarnessType.GROK) == GROK_CATALOG
    assert GROK_CATALOG.switch_mode is SwitchMode.EAGER_THEN_RECONCILE
    assert GROK_CATALOG.picker_mode is PickerMode.LIST
    assert GROK_CATALOG.powered_by_text == "Powered by Grok Build"
    assert GROK_CATALOG.native_atomic_shoulder_tap_possible is True


def test_every_model_matches_a_live_read_at_each_of_its_efforts() -> None:
    for option in GROK_CATALOG.options:
        for choice in option.efforts:
            identity = ModelIdentity(model_id=option.id, effort=choice.level, fast=False)
            assert match_option(identity, GROK_CATALOG.options) == option
    grok_4_5_at_xhigh = ModelIdentity(model_id="grok-4.5", effort="xhigh", fast=False)
    assert match_option(grok_4_5_at_xhigh, GROK_CATALOG.options) is None


def test_a_pick_may_leave_the_effort_unnamed_to_run_at_the_models_default() -> None:
    assert validate_model_pick(GROK_CATALOG.options, "grok-4.5", None, False).id == "grok-4.5"


def test_the_shared_reader_reads_the_file_mngr_grok_writes(tmp_path: Path) -> None:
    assert get_model_state_path(HarnessType.GROK, tmp_path) == get_grok_model_state_path(tmp_path)


def test_the_bar_has_no_slots_until_the_status_line_first_runs(tmp_path: Path) -> None:
    model_state_path = get_model_state_path(HarnessType.GROK, tmp_path)
    assert read_model_identity(model_state_path) is None

    model_state_path.write_text(json.dumps({"model": "grok-4.6", "effort": "xhigh", "fast": False}) + "\n")

    identity = read_model_identity(model_state_path)
    assert identity == ModelIdentity(model_id="grok-4.6", effort="xhigh", fast=False)
    assert resolve_model_choice(identity, GROK_CATALOG.options).matched == GROK_CATALOG.options[0]


def test_a_switch_holds_one_line_per_changed_axis_for_the_flush_worker(tmp_path: Path) -> None:
    held: list[tuple[Path, str]] = []
    resolver = GrokModelResolver.build(_agent_info(tmp_path), hold_control_command=lambda *line: held.append(line))

    def typist(_line: str) -> bool:
        raise AssertionError("grok's only typist is the flush worker")

    result = resolver.switch(
        ModelIdentity(model_id="grok-4.5", effort="low", fast=False),
        frozenset({ModelAxis.MODEL, ModelAxis.EFFORT, ModelAxis.FAST}),
        typist,
    )

    assert result.ok is True
    assert held == [(tmp_path, "/model grok-4.5"), (tmp_path, "/effort low")]


def test_a_switch_holds_only_the_axes_that_changed(tmp_path: Path) -> None:
    held: list[str] = []
    resolver = GrokModelResolver.build(
        _agent_info(tmp_path), hold_control_command=lambda _dir, line: held.append(line)
    )

    resolver.switch(
        ModelIdentity(model_id="grok-4.6", effort="xhigh", fast=False), frozenset({ModelAxis.EFFORT}), _fail
    )
    resolver.switch(ModelIdentity(model_id="grok-4.6", effort=None, fast=False), frozenset({ModelAxis.EFFORT}), _fail)
    resolver.switch(ModelIdentity(model_id="grok-4.6", effort=None, fast=False), frozenset({ModelAxis.FAST}), _fail)

    assert held == ["/effort xhigh"]


def _fail(_line: str) -> bool:
    raise AssertionError("unused")


def test_the_registered_resolver_holds_its_lines_in_the_agents_control_lane(tmp_path: Path) -> None:
    state_dir = tmp_path / "agent-00000000000000000000000000000042"
    state_dir.mkdir()
    try:
        build_resolver(_agent_info(state_dir)).switch(
            ModelIdentity(model_id="grok-4.5", effort="medium", fast=False),
            frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
            _fail,
        )

        tracker = get_tracker(state_dir)
        assert tracker.has_held_work()
        assert tracker.snapshot() == []
        assert tracker.next_control_command() == "/model grok-4.5"
    finally:
        drop_tracker(state_dir.name)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (_LISTED_FOR_AN_API_KEY, ("grok-4.6", "grok-build", "stub-model")),
        (_LISTED_FOR_NO_CREDENTIAL, ()),
        (_LISTED_FOR_A_CUSTOM_MODEL, ("stub",)),
        ("", ()),
    ],
    ids=["api_key", "no_credential", "custom_model", "no_output"],
)
def test_grok_models_output_parses_to_its_model_ids(output: str, expected: tuple[str, ...]) -> None:
    assert parse_grok_models_output(output) == expected


def test_an_account_is_offered_the_catalog_models_grok_lists_for_it(tmp_path: Path) -> None:
    options = GrokModelResolver.list_account_options(
        tmp_path, _runner_returning(_Finished(stdout=_LISTED_FOR_AN_API_KEY))
    )

    assert options == (GROK_CATALOG.options[0],)


@pytest.mark.parametrize(
    "finished",
    [
        _Finished(stdout=_LISTED_FOR_NO_CREDENTIAL),
        _Finished(stdout=_LISTED_FOR_A_CUSTOM_MODEL),
        _Finished(returncode=1, stdout=_LISTED_FOR_AN_API_KEY),
        _Finished(is_timed_out=True),
    ],
    ids=["lists_none", "lists_no_catalog_model", "fails", "times_out"],
)
def test_an_account_grok_gives_no_usable_listing_for_is_offered_the_whole_catalog(
    tmp_path: Path, finished: _Finished
) -> None:
    assert GrokModelResolver.list_account_options(tmp_path, _runner_returning(finished)) is None


def test_an_account_grok_cannot_run_for_is_offered_the_whole_catalog(tmp_path: Path) -> None:
    def run(**_kwargs: Any) -> _Finished:
        raise ProcessError(command=("grok", "models"), stdout="", stderr="no such binary")

    assert list_account_model_ids(tmp_path, run) is None
    assert GrokModelResolver.list_account_options(tmp_path, run) is None


def test_grok_models_runs_bound_to_the_account_without_the_servers_own_grok_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "XAI_API_KEY": "xai-server-key",
        "GROK_AUTH_PROVIDER_COMMAND": "/usr/local/bin/print-token",
        "GROK_MODELS_BASE_URL": "http://127.0.0.1:9/v1",
        "GROK_HOME": "/home/user/.grok",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = build_grok_models_environment(tmp_path)

    assert env["GROK_HOME"] == str(tmp_path)
    assert env["GROK_AUTH_PATH"] == str(tmp_path / "auth.json")
    assert env["GROK_MANAGED_CONFIG"] == "0"
    assert {"XAI_API_KEY", "GROK_AUTH_PROVIDER_COMMAND", "GROK_MODELS_BASE_URL"}.isdisjoint(env)
    assert env["PATH"] == "/usr/bin:/bin"


def test_an_account_holding_only_an_api_key_lists_with_it_as_xai_api_key(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    account_dir = _write_api_key_account(tmp_path / "account", key="xai-key-0b7a")

    list_account_model_ids(account_dir, _runner_returning(_Finished(stdout=_LISTED_FOR_AN_API_KEY), seen))

    assert seen["command"] == ["grok", "models"]
    assert seen["env"]["XAI_API_KEY"] == "xai-key-0b7a"


def test_an_account_with_a_session_login_lists_without_an_xai_api_key(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(json.dumps({"https://auth.x.ai::client-id": {"key": "token"}}))

    assert "XAI_API_KEY" not in build_grok_models_environment(tmp_path)


def test_an_agent_is_offered_the_models_its_bound_account_lists(tmp_path: Path) -> None:
    account_dir = tmp_path / "accounts" / "acct-1"
    account_dir.mkdir(parents=True)
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    (state_dir / "env").write_text(f"MNGR_AGENT_ID=agent-1\nGROK_AUTH_PATH={account_dir / 'auth.json'}\n")
    seen: dict[str, Any] = {}
    runner = _runner_returning(_Finished(stdout=_LISTED_FOR_AN_API_KEY), seen)

    offered = GrokModelResolver.build(_agent_info(state_dir), runner=runner).list_offered_models()

    assert offered == ("grok-4.6",)
    assert seen["env"]["GROK_HOME"] == str(account_dir)


def test_an_agent_bound_to_no_account_is_offered_the_whole_catalog(tmp_path: Path) -> None:
    (tmp_path / "env").write_text("MNGR_AGENT_ID=agent-1\n")

    assert read_agent_account_dir(tmp_path) is None
    assert read_agent_account_dir(tmp_path / "missing") is None
    assert GrokModelResolver.build(_agent_info(tmp_path), runner=_no_runner).list_offered_models() is None
