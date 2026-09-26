"""Tests for ``write_plan.sh``, the detached plan recorder.

Every plan it writes is discarded, so what matters is that it never fails its
caller and that its plan carries the do-not-use header. The planner a build
actually runs is a separate script with its own tests, at
``.agents/skills/build-app/scripts/run_planner_test.py``.

A fake ``claude`` on ``PATH`` stands in for the planner, so no model is called.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

_SCRIPT = Path(__file__).parent / "write_plan.sh"
_SCRIPT_TIMEOUT_SECONDS = 30
_DETACHED_PLAN_WAIT_SECONDS = 8.0
_POLL_INTERVAL_SECONDS = 0.1
_FAKE_PLAN = "<output>fake plan</output>"


def _environment_with_fake_claude(tmp_path: Path, claude_script: str) -> dict[str, str]:
    """An environment whose ``claude`` is ``claude_script`` and whose work dir is under tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(f"#!/bin/sh\ncat >/dev/null\n{claude_script}\n")
    fake_claude.chmod(0o755)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MNGR_")
    }
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["MNGR_AGENT_WORK_DIR"] = str(work_dir)
    return environment


def _run_script(
    arguments: list[str], environment: dict[str, str], stdin_text: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_SCRIPT), *arguments],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=environment,
        timeout=_SCRIPT_TIMEOUT_SECONDS,
        check=False,
    )


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + _DETACHED_PLAN_WAIT_SECONDS
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)


def test_it_records_a_headed_plan_and_returns_at_once(tmp_path: Path) -> None:
    environment = _environment_with_fake_claude(tmp_path, f"echo '{_FAKE_PLAN}'")

    result = _run_script(["build-app"], environment, "Build a to-do list app.\n")

    assert result.returncode == 0
    relative_run_dir = result.stdout.strip()
    assert relative_run_dir.startswith("data/.imbue/plans/build-app/")
    plan_path = Path(environment["MNGR_AGENT_WORK_DIR"]) / relative_run_dir / "plan.md"
    _wait_for(plan_path)
    plan_text = plan_path.read_text()
    assert plan_text.startswith("> DO NOT USE THIS PLAN.")
    assert plan_text.rstrip().endswith(_FAKE_PLAN)


def test_a_failing_planner_still_exits_0_and_keeps_its_output(tmp_path: Path) -> None:
    """The recorder must never fail the agent that started it, and must say why."""
    environment = _environment_with_fake_claude(
        tmp_path, "echo 'Error: budget exceeded'; exit 1"
    )

    result = _run_script(["build-app"], environment, "Build a to-do list app.\n")

    assert result.returncode == 0
    run_dir = Path(environment["MNGR_AGENT_WORK_DIR"]) / result.stdout.strip()
    _wait_for(run_dir / "meta.json")
    assert not (run_dir / "plan.md").exists()
    assert "Error: budget exceeded" in (run_dir / "log").read_text()
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "claude_failed"


def test_an_unknown_flow_exits_0_and_writes_nothing(tmp_path: Path) -> None:
    environment = _environment_with_fake_claude(tmp_path, f"echo '{_FAKE_PLAN}'")

    result = _run_script(["no-such-flow"], environment, "Build something.\n")

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not (Path(environment["MNGR_AGENT_WORK_DIR"]) / "data").exists()
