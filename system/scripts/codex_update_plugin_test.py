"""The provisioning installer must target the new agent, never its parent/account."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from imbue.mngr_codex.codex_config import get_codex_home

_SCRIPT = Path(__file__).with_name("codex_update_plugin.sh")
_SHIM = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
with open(os.environ["SHIM_LOG"], "a") as log:
    log.write(json.dumps([os.environ["CODEX_HOME"], sys.argv[1:]]) + "\\n")
if " ".join(sys.argv[1:3]) == os.environ.get("SHIM_FAIL"):
    sys.exit(1)
if sys.argv[1:3] == ["plugin", "add"]:
    with (Path(os.environ["CODEX_HOME"]) / "config.toml").open("a") as config:
        config.write('\\n[plugins."imbue-code-guardian@imbue-code-guardian"]\\nenabled = true\\n')
"""


def _run(
    tmp_path: Path,
    *args: str,
    fail: str = "",
    provisioned: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[list[object]], Path]:
    state = tmp_path / "agent state"
    home = get_codex_home(state)
    home.mkdir(parents=True, exist_ok=True)
    if provisioned:
        (home / "config.toml").write_text('model = "example"\n')
    shared = tmp_path / "account home"
    shared.mkdir(exist_ok=True)
    (shared / "config.toml").write_text("# untouched\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "codex"
    shim.write_text(_SHIM)
    shim.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", str(_SCRIPT), *args],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "MNGR_AGENT_STATE_DIR": str(state),
            "CODEX_HOME": str(shared),
            "SHIM_LOG": str(log),
            "SHIM_FAIL": fail,
            "CODE_GUARDIAN_MARKETPLACE": "imbue-ai/code-guardian@main",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (shared / "config.toml").read_text() == "# untouched\n"
    return (
        result,
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else [],
        home,
    )


def test_install_and_reprovision_target_mngrs_home(tmp_path: Path) -> None:
    for _ in range(2):
        result, calls, home = _run(tmp_path)
        assert result.returncode == 0, result.stderr
        assert all(call[0] == str(home) for call in calls)
        assert calls[-1][1] == [
            "plugin",
            "add",
            "imbue-code-guardian@imbue-code-guardian",
        ]
        assert "enabled = true" in (home / "config.toml").read_text()
        assert 'model = "example"' in (home / "config.toml").read_text()


@pytest.mark.parametrize("args,expected", [((), 0), (("--strict",), 1)])
def test_install_outage_warns_and_strict_fails(
    tmp_path: Path, args: tuple[str, ...], expected: int
) -> None:
    result, _, _ = _run(tmp_path, *args, fail="plugin add")
    assert result.returncode == expected
    assert "installation failed" in result.stderr


def test_marketplace_outage_still_attempts_cached_install(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, "--strict", fail="plugin marketplace")
    assert result.returncode == 0, result.stderr
    assert "cached marketplace" in result.stderr
    assert calls[-1][1] == ["plugin", "add", "imbue-code-guardian@imbue-code-guardian"]


def test_refuses_install_before_mngr_writes_config(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, provisioned=False)
    assert result.returncode == 1
    assert "must be provisioned" in result.stderr
    assert calls == []


def test_refuses_install_without_agent_state(tmp_path: Path) -> None:
    env = {
        key: value for key, value in os.environ.items() if key != "MNGR_AGENT_STATE_DIR"
    }
    result = subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "refusing a shared-home install" in result.stderr


@pytest.mark.parametrize("args", [("--unknown",), ("--strict", "extra")])
def test_rejects_invalid_arguments(tmp_path: Path, args: tuple[str, ...]) -> None:
    result, calls, _ = _run(tmp_path, *args)
    assert result.returncode == 2
    assert calls == []
