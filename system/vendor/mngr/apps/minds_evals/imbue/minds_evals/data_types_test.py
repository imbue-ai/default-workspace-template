from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.minds_evals.data_types import CapturedFile
from imbue.minds_evals.data_types import HarnessConfig
from imbue.minds_evals.data_types import HarnessLane
from imbue.minds_evals.data_types import HarnessName
from imbue.minds_evals.data_types import ScriptedFlowAction
from imbue.minds_evals.data_types import ScriptedFlowActionKind
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.data_types import harness_for_lane
from imbue.minds_evals.data_types import harness_id
from imbue.minds_evals.data_types import is_same_skill
from imbue.minds_evals.data_types import lane_id


def _flow_check_json(start_path: str, script: Sequence[Mapping[str, object]] = ()) -> dict[str, object]:
    return {
        "check_id": "ui_flow_0_f",
        "name": "f",
        "actions": "Add a task.",
        "script": list(script),
        "expect": "The task is listed.",
        "surface": "origin",
        "start_path": start_path,
    }


def test_a_scripted_flow_check_read_back_from_a_case_keeps_its_script() -> None:
    script = [
        {"kind": "click", "role": "button", "target": "Add"},
        {"kind": "open", "text": "?latency=300"},
    ]
    check = UiFlowCheck.model_validate(_flow_check_json("", script))

    read_back = UiFlowCheck.model_validate_json(check.model_dump_json())

    assert read_back.script == (
        ScriptedFlowAction(kind=ScriptedFlowActionKind.CLICK, role="button", target="Add"),
        ScriptedFlowAction(kind=ScriptedFlowActionKind.OPEN, text="?latency=300"),
    )


@pytest.mark.parametrize(
    ("entry", "expected_message"),
    [
        pytest.param({"kind": "click", "role": "button"}, "'click' needs target", id="missing-field"),
        pytest.param({"kind": "wait", "text": "soon"}, "'wait' does not take text", id="unused-field"),
        pytest.param({"kind": "open", "text": "//evil.example/"}, "is not a start path", id="open-another-host"),
        pytest.param(
            {"kind": "click", "role": "checkbox", "target": "walk dog", "beside": "walk dog"},
            "not both",
            id="target-and-beside",
        ),
    ],
)
def test_a_flow_check_read_back_from_a_case_refuses_an_action_its_kind_cannot_perform(
    entry: dict[str, object], expected_message: str
) -> None:
    # The driver reads its flows back out of the generated task, so a script edited in there meets
    # the same rule generation applied.
    with pytest.raises(ValidationError, match=expected_message):
        UiFlowCheck.model_validate(_flow_check_json("", [entry]))


def test_a_flow_check_read_back_from_a_case_keeps_its_start_path() -> None:
    check = UiFlowCheck.model_validate(_flow_check_json("?latency=300"))

    assert UiFlowCheck.model_validate_json(check.model_dump_json()).start_path == "?latency=300"


def test_a_flow_check_read_back_from_a_case_refuses_a_start_path_that_leaves_the_origin() -> None:
    # The driver reads its flows back out of the generated task, so a start path edited in there
    # meets the same rule generation applied.
    with pytest.raises(ValidationError, match="is not a start path"):
        UiFlowCheck.model_validate(_flow_check_json("//evil.example/"))


def test_captured_file_refuses_a_capture_that_also_names_a_failure() -> None:
    with pytest.raises(ValidationError, match="cannot also carry a failure"):
        CapturedFile(host_path=Path("/logs/agent/verification/x"), failure_reason="pull_failed", failure_detail="")


def test_captured_file_refuses_an_uncaptured_file_without_a_reason() -> None:
    with pytest.raises(ValidationError, match="must name a failure reason"):
        CapturedFile(host_path=None, failure_reason="", failure_detail="")


def test_every_lane_is_spelled_the_way_the_command_line_and_the_workspace_spell_it() -> None:
    """The enum member names carry underscores and the ids do not, so the two would drift silently:
    a lane sent to the accounts API under the wrong spelling is a sign-in the workspace refuses."""
    assert {lane_id(lane) for lane in HarnessLane} == {
        "anthropic",
        "openai",
        "api-key",
        "openrouter",
        "opencode-go",
    }


def test_every_harness_is_spelled_the_way_the_workspace_lists_it() -> None:
    """A trial's arm records the harness as the accounts listing names it, so a harness compared
    against that record under another spelling never matches."""
    assert {harness_id(harness) for harness in HarnessName} == {"claude", "codex", "pi-coding"}


def test_each_lane_runs_the_harness_the_workspace_template_assigns_it() -> None:
    assert {lane_id(lane): harness_id(harness_for_lane(lane)) for lane in HarnessLane} == {
        "anthropic": "claude",
        "openai": "codex",
        "api-key": "pi-coding",
        "openrouter": "pi-coding",
        "opencode-go": "pi-coding",
    }


def test_a_harness_config_requests_a_switch_exactly_when_it_names_a_model() -> None:
    """The model is what the workspace's model endpoint needs before it will apply any axis, so a
    config without one asks for nothing and leaves every setting of the workspace alone."""
    switching = HarnessConfig(
        lane=HarnessLane.ANTHROPIC,
        key_provider="",
        key_env="ANTHROPIC_API_KEY",
        model="haiku",
        effort="medium",
        is_fast=False,
    )

    assert switching.is_switch_requested
    assert not switching.model_copy_update(to_update(switching.field_ref().model, "")).is_switch_requested


@pytest.mark.parametrize(
    ("requested", "invoked", "is_match"),
    [
        ("frontend-design", "frontend-design", True),
        ("frontend-design", "frontend-design:frontend-design", True),
        ("frontend-design", "some-plugin:frontend-design", True),
        ("frontend-design:frontend-design", "frontend-design:frontend-design", True),
        # A qualified request is exact: a bare invocation is not the plugin's skill.
        ("frontend-design:frontend-design", "frontend-design", False),
        ("design", "frontend-design", False),
        ("frontend-design", "frontend-design-v2", False),
    ],
)
def test_is_same_skill_reads_plugin_qualified_names(requested: str, invoked: str, is_match: bool) -> None:
    assert is_same_skill(requested, invoked) is is_match
