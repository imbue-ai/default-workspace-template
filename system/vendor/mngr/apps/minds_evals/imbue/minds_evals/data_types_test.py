from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.minds_evals.data_types import CapturedFile


def test_captured_file_refuses_a_capture_that_also_names_a_failure() -> None:
    with pytest.raises(ValidationError, match="cannot also carry a failure"):
        CapturedFile(host_path=Path("/logs/agent/verification/x"), failure_reason="pull_failed", failure_detail="")


def test_captured_file_refuses_an_uncaptured_file_without_a_reason() -> None:
    with pytest.raises(ValidationError, match="must name a failure reason"):
        CapturedFile(host_path=None, failure_reason="", failure_detail="")
