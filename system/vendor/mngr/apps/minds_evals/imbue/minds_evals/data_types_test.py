from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.imbue_common.model_update import to_update
from imbue.minds_evals.data_types import CapturedFile
from imbue.minds_evals.data_types import HarnessConfig
from imbue.minds_evals.data_types import HarnessLane
from imbue.minds_evals.data_types import lane_id


def test_captured_file_refuses_a_capture_that_also_names_a_failure() -> None:
    with pytest.raises(ValidationError, match="cannot also carry a failure"):
        CapturedFile(host_path=Path("/logs/agent/verification/x"), failure_reason="pull_failed", failure_detail="")


def test_captured_file_refuses_an_uncaptured_file_without_a_reason() -> None:
    with pytest.raises(ValidationError, match="must name a failure reason"):
        CapturedFile(host_path=None, failure_reason="", failure_detail="")


def test_every_lane_is_spelled_the_way_the_command_line_and_the_workspace_spell_it() -> None:
    """The enum member names carry underscores and the ids do not, so the two would drift silently:
    a lane sent to the accounts API under the wrong spelling is a sign-in the workspace refuses."""
    assert {lane_id(lane) for lane in HarnessLane} == {"anthropic", "api-key", "openrouter", "opencode-go"}


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
