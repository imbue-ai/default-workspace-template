"""Tests for ``run_planner.sh``, the planner one build-app run depends on.

This is the build's critical path: the exit code is what the orchestrator reads
to decide whether it has a plan, so each of the three outcomes is pinned here --
a plan written (0), a planner that produced nothing (1), and a call that could
not start (2).

A fake ``claude`` on ``PATH`` stands in for the planner, so no model is called.

Run via: ``uv run pytest .agents/skills/build-app/scripts/run_planner_test.py``
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).parent / "run_planner.sh"
_SCRIPT_TIMEOUT_SECONDS = 30
_FAKE_PLAN = "<output>fake plan</output>"


def _environment_with_fake_claude(
    tmp_path: Path, claude_script: str, *, drain_stdin: bool = True
) -> dict[str, str]:
    """An environment whose ``claude`` is ``claude_script`` and whose work dir is under tmp_path.

    The prompt arrives on the planner's stdin, so the fake drains it by default.
    A script that wants to read the prompt passes ``drain_stdin=False``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    drain = "cat >/dev/null\n" if drain_stdin else ""
    fake_claude.write_text(f"#!/bin/sh\n{drain}{claude_script}\n")
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
    run_dir: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_SCRIPT), str(run_dir)],
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


def test_it_writes_the_plan_with_no_recorder_header(tmp_path: Path) -> None:
    """The plan here is the one the build runs, so it carries no do-not-use header."""
    environment = _environment_with_fake_claude(tmp_path, f"echo '{_FAKE_PLAN}'")
    run_dir = _make_run_dir(tmp_path)

    result = _run_script(run_dir, environment)

    assert result.returncode == 0, result.stderr
    plan_text = (run_dir / "plan.md").read_text()
    assert "DO NOT USE" not in plan_text
    assert plan_text.rstrip().endswith(_FAKE_PLAN)
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "ok"


def test_it_feeds_the_skills_own_prompt_and_the_brief_to_the_planner(
    tmp_path: Path,
) -> None:
    # The fake claude keeps its stdin, which is the prompt the script built.
    fed = tmp_path / "fed.txt"
    environment = _environment_with_fake_claude(
        tmp_path, f"cat >'{fed}'\necho '{_FAKE_PLAN}'", drain_stdin=False
    )
    run_dir = _make_run_dir(tmp_path)

    assert _run_script(run_dir, environment).returncode == 0

    fed_text = fed.read_text()
    prompt = (
        Path(__file__).parent.parent / "references" / "planner-prompt.md"
    ).read_text()
    assert prompt.strip() in fed_text
    assert "Build a to-do list app." in fed_text


def test_a_failing_planner_exits_1_and_keeps_its_output(tmp_path: Path) -> None:
    environment = _environment_with_fake_claude(
        tmp_path, "echo 'Error: budget exceeded'; exit 1"
    )
    run_dir = _make_run_dir(tmp_path)

    result = _run_script(run_dir, environment)

    assert result.returncode == 1
    assert not (run_dir / "plan.md").exists()
    assert "Error: budget exceeded" in (run_dir / "log").read_text()
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "planner_failed"


def test_a_silent_planner_exits_1(tmp_path: Path) -> None:
    environment = _environment_with_fake_claude(tmp_path, "true")
    run_dir = _make_run_dir(tmp_path)

    result = _run_script(run_dir, environment)

    assert result.returncode == 1
    assert not (run_dir / "plan.md").exists()
    assert json.loads((run_dir / "meta.json").read_text())["status"] == "empty_output"


def test_it_refuses_a_missing_brief_a_missing_folder_or_an_existing_plan(
    tmp_path: Path,
) -> None:
    environment = _environment_with_fake_claude(tmp_path, f"echo '{_FAKE_PLAN}'")
    empty_run_dir = tmp_path / "empty"
    empty_run_dir.mkdir()
    run_dir = _make_run_dir(tmp_path)
    (run_dir / "plan.md").write_text("an earlier plan\n")

    missing_brief = _run_script(empty_run_dir, environment)
    no_folder = _run_script(tmp_path / "nope", environment)
    existing_plan = _run_script(run_dir, environment)

    assert missing_brief.returncode == 2
    assert "no brief" in missing_brief.stderr
    assert no_folder.returncode == 2
    assert "no run folder" in no_folder.stderr
    assert existing_plan.returncode == 2
    assert "already exists" in existing_plan.stderr
    # An existing plan is never replaced.
    assert (run_dir / "plan.md").read_text() == "an earlier plan\n"
