"""Tests for the in-workspace backup provisioning script.

The pure parsing and the missing-key guard are exercised directly; the real
``restic init`` round trip runs only when a ``restic`` binary is available (a
local filesystem repository needs no network), and is skipped otherwise.
"""

import importlib.util
import shutil
import uuid
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parent / "provision_backups.py"


def _load_provision_backups() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_provision_backups_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_provision_backups()


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


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic binary not installed")
def test_initialize_repository_is_idempotent_against_a_local_repo(tmp_path: Path) -> None:
    repo_dir = tmp_path / f"repo-{uuid.uuid4().hex}"
    env_values = {"RESTIC_REPOSITORY": f"local:{repo_dir}", "RESTIC_PASSWORD": uuid.uuid4().hex}

    assert _MODULE.initialize_repository(env_values) is True
    # A second init against the same repository reports "already initialized".
    assert _MODULE.initialize_repository(env_values) is False
