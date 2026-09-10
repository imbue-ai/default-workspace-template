"""The create gate as mngr runs it, end to end through the real vendored `mngr create`.

The unit tests beside this file (`require_create_account_test.py`) run the script directly
and settle what it decides. What they cannot show is that mngr reaches it at all: the gate is
a `pre_command_scripts.create` entry in the committed `.mngr/settings.toml`, and everything
that matters about it -- the shell test that keeps `python3` out of a create run on the
user's own machine, the project root mngr runs the entry from, and the script's stderr
reaching the user inside mngr's own refusal -- lives in that entry rather than in the script.
So these run the real thing, which costs a `uv run mngr create` apiece.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("require_create_account.py")
_spec = importlib.util.spec_from_file_location(
    "require_create_account_for_e2e", _SCRIPT
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
NO_ACCOUNT_MESSAGE: str = _module.NO_ACCOUNT_MESSAGE

_COMMITTED_SETTINGS = _SCRIPT.parents[2] / ".mngr" / "settings.toml"


def _inside_workspace(cwd: Path) -> dict[str, str]:
    return {"MNGR_AGENT_ID": "agent-1", "MNGR_AGENT_WORK_DIR": str(cwd)}


def _committed_pre_command_entry() -> str:
    """The `pre_command_scripts.create` entry of the committed settings, so the test runs the real one."""
    (entry,) = tomllib.loads(_COMMITTED_SETTINGS.read_text())["pre_command_scripts"][
        "create"
    ]
    return entry


def _mngr_create_in_a_gated_project(
    tmp_path: Path, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the real vendored `mngr create` in a temp project carrying the committed gate entry.

    The entry names the script by its project-relative path, so the script is copied to that
    path inside the project; mngr runs the entry from the project root, in this environment.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    project = tmp_path / "project"
    (project / ".mngr").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    script_copy = project / _SCRIPT.relative_to(_SCRIPT.parents[2])
    script_copy.parent.mkdir(parents=True)
    shutil.copy(_SCRIPT, script_copy)
    (project / ".mngr" / "settings.toml").write_text(
        f"is_allowed_in_pytest = true\n\n[pre_command_scripts]\ncreate = [{_committed_pre_command_entry()!r}]\n"
    )
    return subprocess.run(
        ["uv", "run", "mngr", "create", "gated", "--transfer", "none", "--no-connect"],
        cwd=project,
        env={**os.environ, "MNGR_HOST_DIR": str(tmp_path / "host"), **extra_env},
        capture_output=True,
        text=True,
        check=False,
        timeout=110,
    )


@pytest.mark.timeout(120)
def test_mngr_refuses_the_create_quoting_the_message(tmp_path: Path) -> None:
    """The gate as mngr runs it: the committed `pre_command_scripts.create` entry, from the project root, in
    the create's own environment, whose failure aborts the create with the script's stderr in the error."""
    result = _mngr_create_in_a_gated_project(
        tmp_path, _inside_workspace(tmp_path / "project")
    )

    assert result.returncode != 0
    assert NO_ACCOUNT_MESSAGE in result.stderr
    assert "Pre-command script(s) failed for 'create'" in result.stderr


@pytest.mark.timeout(120)
def test_the_create_of_the_workspace_itself_never_reaches_the_gate(
    tmp_path: Path,
) -> None:
    """Outside any agent the committed entry's shell test short-circuits before python3 is even named, so a
    machine with no python3 creates workspaces all the same; the create then fails on mngr's own terms."""
    result = _mngr_create_in_a_gated_project(tmp_path, {})

    assert result.returncode != 0
    assert NO_ACCOUNT_MESSAGE not in result.stderr
    assert "Pre-command script(s) failed" not in result.stderr
    assert "No agent type provided" in result.stderr
