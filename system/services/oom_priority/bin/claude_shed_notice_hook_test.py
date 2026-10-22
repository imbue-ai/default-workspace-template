"""The shed-notice SessionStart hook starts Python only when the ledger names the agent.

Starting Python is the slowest part of the hook, and a session's first message waits
for it, so the shell front must skip it whenever there is nothing to say -- and must
still deliver the notice when there is. A ``python3`` shim on PATH records whether
Python was started at all, then hands over to the real interpreter.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from oom_priority.ledger import append_shed_record

_HOOK = Path(__file__).with_name("claude_shed_notice_hook.sh")
_AGENT = "Chat-3"


def _python_shim(tmp_path: Path) -> Path:
    real_python = shutil.which("python3")
    assert real_python is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "python3"
    shim.write_text(
        f'#!/usr/bin/env bash\necho started >> "{tmp_path}/python.log"\nexec "{real_python}" "$@"\n'
    )
    shim.chmod(0o755)
    return bin_dir


def _write_ledger(
    monkeypatch: pytest.MonkeyPatch, runtime_dir: Path, agent_names: list[str | None]
) -> None:
    """Record a shed of each named agent (None: a subprocess) through the kill hook's own writer."""
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(runtime_dir))
    for idx, name in enumerate(agent_names):
        append_shed_record(
            pid=100 + idx,
            comm="claude",
            agent_name=name,
            is_worker=None if name is None else False,
        )


def _run_hook(tmp_path: Path, runtime_dir: Path) -> tuple[str, bool]:
    result = subprocess.run(
        ["bash", str(_HOOK)],
        env={
            **os.environ,
            "PATH": f"{_python_shim(tmp_path)}{os.pathsep}{os.environ['PATH']}",
            "MNGR_AGENT_NAME": _AGENT,
            "OOM_PRIORITY_RUNTIME_DIR": str(runtime_dir),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout, (tmp_path / "python.log").exists()


def test_no_ledger_means_no_python(tmp_path: Path) -> None:
    stdout, is_python_started = _run_hook(tmp_path, tmp_path / "runtime")

    assert stdout == ""
    assert not is_python_started


def test_a_ledger_naming_only_other_agents_means_no_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    _write_ledger(monkeypatch, runtime_dir, ["Chat-30", None])

    stdout, is_python_started = _run_hook(tmp_path, runtime_dir)

    assert stdout == ""
    assert not is_python_started


def test_a_pending_shed_of_this_agent_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    _write_ledger(monkeypatch, runtime_dir, [_AGENT])

    stdout, is_python_started = _run_hook(tmp_path, runtime_dir)

    assert is_python_started
    assert "memory-pressure" in stdout
