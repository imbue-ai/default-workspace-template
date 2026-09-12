"""A worker's lead address survives a rename because it is the lead's agent id.

`create_worker.py launch` stamps `lead_agent` with the lead's id, and `worker-reporting.md`
has the worker read `mngr transcript $LEAD_AGENT`. This runs the real vendored mngr to pin
the fact that rests on: an agent stays reachable by its id across a `mngr rename`, and is
not reachable by its old name. It costs a `uv run mngr create`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _mngr(
    project: Path, host_dir: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "mngr", *args],
        cwd=project,
        env={**os.environ, "MNGR_HOST_DIR": str(host_dir)},
        capture_output=True,
        text=True,
        check=False,
        timeout=110,
    )


@pytest.mark.timeout(240)
def test_an_agent_stays_addressable_by_id_across_a_rename(tmp_path: Path) -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    project = tmp_path / "project"
    (project / ".mngr").mkdir(parents=True)
    (project / ".mngr" / "settings.toml").write_text("is_allowed_in_pytest = true\n")
    (project / "README.md").write_text("lead\n")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    # `mngr create` refuses a dirty tree, so the seed files are committed.
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=project,
        check=True,
    )
    host_dir = tmp_path / "host"

    created = _mngr(
        project,
        host_dir,
        "create",
        "lead-before",
        "--type",
        "command",
        "--transfer",
        "none",
        "--no-connect",
        "--format",
        "jsonl",
        "--",
        "sleep",
        "73912",
    )
    assert created.returncode == 0, created.stderr
    events = [
        json.loads(line) for line in created.stdout.splitlines() if line.startswith("{")
    ]
    agent_id = next(
        event["agent_id"] for event in events if event.get("event") == "created"
    )
    try:
        renamed = _mngr(project, host_dir, "rename", "lead-before", "lead-after")
        assert renamed.returncode == 0, renamed.stderr

        # The worker's transcript read resolves the agent by id (a command agent has no
        # transcript to show, which is the error mngr answers once it has found the agent).
        transcript = _mngr(project, host_dir, "transcript", agent_id)
        assert "does not produce a common transcript" in transcript.stderr
        assert "Could not find agent" not in transcript.stderr

        # The old name is what a name-stamped worker would read: it is gone.
        by_old_name = _mngr(project, host_dir, "transcript", "lead-before")
        assert by_old_name.returncode != 0
        assert "Could not find agent" in by_old_name.stderr
    finally:
        _mngr(project, host_dir, "destroy", agent_id, "--force")
