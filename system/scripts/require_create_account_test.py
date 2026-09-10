"""Tests for the pre-command gate that turns "no agent type" into "sign in first".

The script is run as a real subprocess with an explicit environment, since the gate is the
environment: which variables mngr sources for a create run inside the workspace, and which
directory that create is run from.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("require_create_account.py")
_spec = importlib.util.spec_from_file_location("require_create_account", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
NO_ACCOUNT_MESSAGE: str = _module.NO_ACCOUNT_MESSAGE


def _run(cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _inside_workspace(cwd: Path) -> dict[str, str]:
    return {"MNGR_AGENT_ID": "agent-1", "MNGR_AGENT_WORK_DIR": str(cwd)}


def _write_local_settings(cwd: Path, body: str) -> None:
    (cwd / ".mngr").mkdir(exist_ok=True)
    (cwd / ".mngr" / "settings.local.toml").write_text(body)


def test_a_create_outside_any_agent_passes(tmp_path: Path) -> None:
    """The create of the workspace itself runs from a checkout of this template on the user's machine."""
    assert _run(tmp_path).returncode == 0


def test_a_create_outside_any_agent_passes_on_a_python_without_tomllib(
    tmp_path: Path,
) -> None:
    """The Minds app runs the workspace's own create from a clone of this template on the user's
    machine, whose `python3` can be the 3.9 of macOS's Command Line Tools: the host-side exit must
    come before anything that needs a newer interpreter."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys\n"
            "sys.modules['tomllib'] = None\n"
            f"sys.argv = [{str(_SCRIPT)!r}]\n"
            f"runpy.run_path({str(_SCRIPT)!r}, run_name='__main__')\n",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_a_create_from_an_agent_outside_its_own_checkout_passes(tmp_path: Path) -> None:
    """A developer's agent creating a workspace from this template's checkout is not a create inside one."""
    result = _run(
        tmp_path,
        MNGR_AGENT_ID="agent-1",
        MNGR_AGENT_WORK_DIR=str(tmp_path / "elsewhere"),
    )
    assert result.returncode == 0


def test_a_create_inside_the_workspace_with_nothing_signed_in_is_refused_with_the_sign_in_message(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, **_inside_workspace(tmp_path))
    assert result.returncode == 1
    assert result.stderr.strip() == NO_ACCOUNT_MESSAGE


def test_a_local_file_naming_a_default_type_lets_the_create_through(
    tmp_path: Path,
) -> None:
    _write_local_settings(tmp_path, '[commands.create]\ntype = "codex"\n')
    assert _run(tmp_path, **_inside_workspace(tmp_path)).returncode == 0


@pytest.mark.parametrize(
    "body",
    (
        "[commands.create]\nconnect = true\n",
        '[commands.create]\ntype = ""\n',
        "not = [toml\n",
    ),
    ids=("no-type", "empty-type", "unreadable"),
)
def test_a_local_file_that_names_no_type_still_refuses(
    tmp_path: Path, body: str
) -> None:
    _write_local_settings(tmp_path, body)
    result = _run(tmp_path, **_inside_workspace(tmp_path))
    assert result.returncode == 1
    assert NO_ACCOUNT_MESSAGE in result.stderr


def test_the_file_is_read_from_mngrs_project_config_override(tmp_path: Path) -> None:
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "settings.local.toml").write_text(
        '[commands.create]\ntype = "claude"\n'
    )
    _write_local_settings(tmp_path, "[commands.create]\nconnect = true\n")

    passed = _run(
        tmp_path, **_inside_workspace(tmp_path), MNGR_PROJECT_CONFIG_DIR=str(config_dir)
    )
    assert passed.returncode == 0


_COMMITTED_SETTINGS = _SCRIPT.parents[2] / ".mngr" / "settings.toml"


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
