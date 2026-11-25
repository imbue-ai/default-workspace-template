"""Integration test: the boot's agent upgrade, run with the workspace venv's real mngr on Claude agents of its own.

The agents run the pinned ``claude`` in an isolated host dir on a private tmux server, talking to a loopback
``StandInAnthropicApi``, so no credential or network is needed.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.testing import (
    environment_without_own_agent_identity,
    real_claude_agent,
    stand_in_real_claude_agent,
)
from loguru import logger

from bootstrap.manager import WORKSPACE_MNGR, _upgrade_stopped_agents

_REPO_ROOT = Path(__file__).resolve().parents[5]
_STAND_IN_API_KEY = "sk-ant-api03-stand-in-boot-upgrade"
# A file mngr generates into every Claude agent; without it the agent reads as made by an older mngr.
_GENERATED_FILE = Path("commands") / "mngr_background_task_lib.sh"
# Keeps the pinned claude from updating itself, as the workspace's own config does.
_PROJECT_SETTINGS = (
    'is_allowed_in_pytest = true\n\n[agent_types.claude]\nupdate_policy = "NEVER"\n'
)


def _mngr(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["mngr", *args],
        env=environment_without_own_agent_identity(),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _states_by_name() -> dict[str, str]:
    listed = _mngr("list", "--provider", "local", "--format", "{name}|{state}")
    assert listed.returncode == 0, listed.stderr
    return dict(line.split("|") for line in listed.stdout.splitlines() if "|" in line)


def _pane_pids(agent: AgentInfo) -> list[str]:
    """The pids of the panes of the agent's tmux session on the test's private server."""
    socket_path = Path(os.environ["TMUX_TMPDIR"]) / f"tmux-{os.getuid()}" / "default"
    panes = subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "list-panes",
            "-a",
            "-F",
            "#{session_name} #{pane_pid}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    pids = sorted(
        line.split()[1] for line in panes if line.split()[0].endswith(agent.name)
    )
    assert pids, panes
    return pids


@contextmanager
def _boot_log() -> Generator[list[str], None, None]:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        yield lines
    finally:
        logger.remove(sink)


@pytest.mark.real_claude
@pytest.mark.timeout(480)
def test_the_boot_upgrades_a_stopped_agent_and_leaves_a_running_one_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    project_config_dir = tmp_path / "project-config"
    project_config_dir.mkdir()
    (project_config_dir / "settings.toml").write_text(_PROJECT_SETTINGS)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(project_config_dir))
    project_dir = tmp_path / "project"
    log_path = tmp_path / "mngr.log"
    with (
        stand_in_real_claude_agent(
            project_dir,
            log_path,
            "boot-running-41",
            lambda _body: "OK",
            _STAND_IN_API_KEY,
            monkeypatch,
        ) as (_api, running),
        real_claude_agent(project_dir, log_path, "boot-stopped-41") as stopped,
    ):
        stopped_run = _mngr("stop", stopped.name)
        assert stopped_run.returncode == 0, stopped_run.stderr
        for agent in (running, stopped):
            (agent.agent_state_dir / _GENERATED_FILE).unlink()
        running_pids = _pane_pids(running)
        monkeypatch.chdir(project_dir)

        with _boot_log() as first_run:
            _upgrade_stopped_agents(_REPO_ROOT / WORKSPACE_MNGR)

        assert _pane_pids(running) == running_pids
        assert not (running.agent_state_dir / _GENERATED_FILE).exists()
        assert any(
            f"mngr upgrade: upgraded {stopped.name} (stopped)" in line
            for line in first_run
        ), first_run
        assert (stopped.agent_state_dir / _GENERATED_FILE).is_file()
        states = _states_by_name()
        assert states[stopped.name] == "STOPPED"
        assert states[running.name] == "WAITING"

        with _boot_log() as second_run:
            _upgrade_stopped_agents(_REPO_ROOT / WORKSPACE_MNGR)

        assert any(
            f"mngr upgrade: current {stopped.name}" in line for line in second_run
        ), second_run
        assert not any("mngr upgrade: upgraded" in line for line in second_run)
