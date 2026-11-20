"""The workspace's cloud-connections switch: one line of the host's env file, as each harness's agents see it."""

import os
from pathlib import Path

import pytest

from imbue.chat.harnesses.claude.cloud_connections import ClaudeCloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.codex.cloud_connections import CodexCloudConnections

_BOTH = pytest.mark.parametrize("connections", [ClaudeCloudConnections(), CodexCloudConnections()])


def _agent(tmp_path: Path, host_env: str | None) -> Path:
    """An agent's state dir whose env file names its host dir, the way mngr writes it."""
    host_dir = tmp_path / "host"
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    if host_env is not None:
        (host_dir / "env").write_text(host_env)
    (agent_state_dir / "env").write_text(f"MNGR_HOST_DIR={host_dir}\n")
    return agent_state_dir


def _start(agent_state_dir: Path, connections: CloudConnections, seconds_after_the_switch: float) -> None:
    """Leave the marker the harness touches when the agent's process starts, timed against the host's env file."""
    marker = agent_state_dir / connections.process_started_marker_filename
    marker.touch()
    started_at = (agent_state_dir.parent.parent / "env").stat().st_mtime + seconds_after_the_switch
    os.utime(marker, (started_at, started_at))


@_BOTH
@pytest.mark.parametrize("off", ["false", "OFF", "0", "no"])
def test_no_agent_loads_the_connectors_while_the_switch_is_off(
    tmp_path: Path, connections: CloudConnections, off: str
) -> None:
    agent_state_dir = _agent(tmp_path, f"ENABLE_CLAUDEAI_MCP_SERVERS={off}\n")
    _start(agent_state_dir, connections, seconds_after_the_switch=60)
    assert not connections.is_enabled(agent_state_dir)


@_BOTH
def test_an_agent_loads_the_connectors_once_it_has_started_with_the_switch_on(
    tmp_path: Path, connections: CloudConnections
) -> None:
    """An agent that was running when the switch was turned on has none of them until it restarts, and neither
    has one that never started."""
    agent_state_dir = _agent(tmp_path, "ENABLE_CLAUDEAI_MCP_SERVERS=true\n")
    assert not connections.is_enabled(agent_state_dir)
    _start(agent_state_dir, connections, seconds_after_the_switch=-60)
    assert not connections.is_enabled(agent_state_dir)
    _start(agent_state_dir, connections, seconds_after_the_switch=60)
    assert connections.is_enabled(agent_state_dir)


@pytest.mark.parametrize("host_env", ["OTHER=1\n", None])
def test_a_host_that_does_not_set_the_switch_leaves_each_harness_at_its_own_default(
    tmp_path: Path, host_env: str | None
) -> None:
    """Claude Code loads the connectors unless told not to; codex is started with the apps off unless told to."""
    agent_state_dir = _agent(tmp_path, host_env)
    assert ClaudeCloudConnections().is_enabled(agent_state_dir)
    assert not CodexCloudConnections().is_enabled(agent_state_dir)


@_BOTH
def test_enabling_rewrites_the_one_line_of_the_hosts_env_and_leaves_the_agents_alone(
    tmp_path: Path, connections: CloudConnections
) -> None:
    agent_state_dir = _agent(tmp_path, "IS_SANDBOX=1\nENABLE_CLAUDEAI_MCP_SERVERS=false\nTICKETS_DIR=/tickets\n")
    agent_env = (agent_state_dir / "env").read_text()

    connections.enable(agent_state_dir)

    host_env = tmp_path / "host" / "env"
    assert host_env.read_text().splitlines() == [
        "IS_SANDBOX=1",
        "TICKETS_DIR=/tickets",
        "ENABLE_CLAUDEAI_MCP_SERVERS=true",
    ]
    assert (agent_state_dir / "env").read_text() == agent_env
    _start(agent_state_dir, connections, seconds_after_the_switch=0)
    assert connections.is_enabled(agent_state_dir)


@_BOTH
def test_enabling_a_switch_that_is_already_on_does_not_touch_it(tmp_path: Path, connections: CloudConnections) -> None:
    """A rewrite would make every agent that started since look as if it had started before the switch."""
    agent_state_dir = _agent(tmp_path, "ENABLE_CLAUDEAI_MCP_SERVERS=true\n")
    _start(agent_state_dir, connections, seconds_after_the_switch=0)
    host_env = tmp_path / "host" / "env"
    before = host_env.stat().st_mtime_ns

    connections.enable(agent_state_dir)

    assert host_env.stat().st_mtime_ns == before
    assert connections.is_enabled(agent_state_dir)


@_BOTH
def test_an_agent_whose_env_file_names_no_host_cannot_be_read_or_switched(
    tmp_path: Path, connections: CloudConnections
) -> None:
    with pytest.raises(CloudConnectionsError, match="names no host"):
        connections.is_enabled(tmp_path)
    with pytest.raises(CloudConnectionsError, match="names no host"):
        connections.enable(tmp_path)
