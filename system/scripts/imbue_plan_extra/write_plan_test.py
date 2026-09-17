"""Tests for ``write_plan.sh``: the foreground mode build-app-parallel uses, and the
detached recorder mode that build-app uses, which must keep behaving as before.

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


def _make_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "brief.md").write_text("Build a to-do list app.\n")
    return run_dir


def test_foreground_writes_the_plan_without_the_recorder_header(tmp_path: Path) -> None:
    environment = _environment_with_fake_claude(tmp_path, f"echo '{_FAKE_PLAN}'")
    run_dir = _make_run_dir(tmp_path)

    result = _run_script(
        ["--run-dir", str(run_dir), "build-app-parallel"], environment, ""
    )

    assert result.returncode == 0, result.stderr
    assert (run_dir / "plan.md").read_text() == f"{_FAKE_PLAN}\n"
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "ok"


def test_foreground_planner_failure_exits_1_and_keeps_its_output(
    tmp_path: Path,
) -> None:
    environment = _environment_with_fake_claude(
        tmp_path, "echo 'Error: budget exceeded'; exit 1"
    )
    run_dir = _make_run_dir(tmp_path)

    result = _run_script(
        ["--run-dir", str(run_dir), "build-app-parallel"], environment, ""
    )

    assert result.returncode == 1
    assert "no plan written (claude_failed)" in result.stderr
    assert not (run_dir / "plan.md").exists()
    assert "Error: budget exceeded" in (run_dir / "log").read_text()
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "claude_failed"


def test_foreground_refuses_a_missing_brief_or_an_existing_plan(
    tmp_path: Path,
) -> None:
    environment = _environment_with_fake_claude(tmp_path, f"echo '{_FAKE_PLAN}'")
    empty_run_dir = tmp_path / "empty"
    empty_run_dir.mkdir()
    run_dir = _make_run_dir(tmp_path)
    (run_dir / "plan.md").write_text("an earlier plan\n")

    missing_brief = _run_script(
        ["--run-dir", str(empty_run_dir), "build-app-parallel"], environment, ""
    )
    existing_plan = _run_script(
        ["--run-dir", str(run_dir), "build-app-parallel"], environment, ""
    )

    assert missing_brief.returncode == 2
    assert "no brief" in missing_brief.stderr
    assert existing_plan.returncode == 2
    assert "already exists" in existing_plan.stderr
    assert (run_dir / "plan.md").read_text() == "an earlier plan\n"


def test_detached_recorder_still_records_a_headed_plan(tmp_path: Path) -> None:
    """The default mode returns at once, prints its run directory, and the detached
    child writes the plan under the do-not-use header."""
    environment = _environment_with_fake_claude(tmp_path, f"echo '{_FAKE_PLAN}'")

    result = _run_script(["build-app"], environment, "Build a to-do list app.\n")

    assert result.returncode == 0
    relative_run_dir = result.stdout.strip()
    assert relative_run_dir.startswith("data/.imbue/plans/build-app/")
    plan_path = Path(environment["MNGR_AGENT_WORK_DIR"]) / relative_run_dir / "plan.md"
    deadline = time.monotonic() + _DETACHED_PLAN_WAIT_SECONDS
    while not plan_path.exists() and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
    plan_text = plan_path.read_text()
    assert plan_text.startswith("> DO NOT USE THIS PLAN.")
    assert plan_text.rstrip().endswith(_FAKE_PLAN)
