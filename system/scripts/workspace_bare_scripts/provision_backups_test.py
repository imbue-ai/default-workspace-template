"""Tests for the in-workspace backup provisioning script.

The pure parsing and the missing-key guard are exercised directly; the real
``restic init`` round trip runs only when a ``restic`` binary is available (a
local filesystem repository needs no network), and is skipped otherwise.
"""

import shutil
import uuid
from pathlib import Path

import pytest

from workspace_bare_scripts import provision_backups as _MODULE


def test_parse_restic_env_file_handles_export_quotes_and_comments() -> None:
    parsed = _MODULE.parse_restic_env_file(
        "# comment\nexport RESTIC_PASSWORD=\"pa ss\"\nRESTIC_REPOSITORY='local:/x'\n\nBAD\n"
    )
    assert parsed == {"RESTIC_PASSWORD": "pa ss", "RESTIC_REPOSITORY": "local:/x"}


def test_initialize_repository_requires_repository_and_password() -> None:
    with pytest.raises(_MODULE.BackupProvisionError, match="missing required keys"):
        _MODULE.initialize_repository({"RESTIC_PASSWORD": "x"})


def test_provision_from_file_raises_when_env_absent(tmp_path: Path) -> None:
    with pytest.raises(_MODULE.BackupProvisionError, match="not found"):
        _MODULE.provision_from_file(tmp_path / "absent.env")


class _ScriptedInit:
    """A ``run_init`` seam returning one scripted outcome and recording the call."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self._returncode = returncode
        self._stderr = stderr
        self.call_count = 0

    def __call__(self, env_values: dict[str, str]) -> _MODULE._InitAttempt:
        self.call_count += 1
        return _MODULE._InitAttempt(self._returncode, "", self._stderr)


def test_initialize_repository_treats_an_existing_repository_as_success() -> None:
    scripted = _ScriptedInit(
        1, "Fatal: repository master key and config already initialized"
    )

    created = _MODULE.initialize_repository(
        {"RESTIC_REPOSITORY": "rest:https://x/u/w/", "RESTIC_PASSWORD": "pw"},
        run_init=scripted,
    )

    assert created is False
    assert scripted.call_count == 1


def test_initialize_repository_surfaces_a_failure_without_retrying() -> None:
    # The backup store's token is valid the moment it is issued, so a refusal is
    # a real failure and is reported at once rather than ridden out.
    scripted = _ScriptedInit(
        1, "Fatal: unexpected HTTP response (401): 401 Unauthorized"
    )

    with pytest.raises(_MODULE.BackupProvisionError, match="exit 1"):
        _MODULE.initialize_repository(
            {"RESTIC_REPOSITORY": "rest:https://x/u/w/", "RESTIC_PASSWORD": "pw"},
            run_init=scripted,
        )

    assert scripted.call_count == 1


@pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not installed"
)
def test_initialize_repository_is_idempotent_against_a_local_repo(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / f"repo-{uuid.uuid4().hex}"
    env_values = {
        "RESTIC_REPOSITORY": f"local:{repo_dir}",
        "RESTIC_PASSWORD": uuid.uuid4().hex,
    }

    assert _MODULE.initialize_repository(env_values) is True
    # A second init against the same repository reports "already initialized".
    assert _MODULE.initialize_repository(env_values) is False
