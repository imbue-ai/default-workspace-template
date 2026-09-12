import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.minds_admin.envs.local_process_preflight import EnvLocalHolders
from imbue.minds_admin.envs.local_process_preflight import RunningProcess
from imbue.minds_admin.envs.local_process_preflight import describe_env_local_holder_lines
from imbue.minds_admin.envs.local_process_preflight import describe_env_local_holders
from imbue.minds_admin.envs.local_process_preflight import desktop_pids_for_env_root
from imbue.minds_admin.envs.local_process_preflight import env_latchkey_plugin_data_dir
from imbue.minds_admin.envs.local_process_preflight import find_env_local_holders
from imbue.minds_admin.envs.local_process_preflight import stop_env_local_processes
from imbue.mngr_latchkey.store import acquire_forward_lock


def _process(pid: int, *argv: str, environ: dict[str, str] | None = None) -> RunningProcess:
    return RunningProcess(pid=pid, argv=argv, environ=environ or {})


def test_desktop_pids_match_only_processes_launched_with_this_envs_client_toml(tmp_path: Path) -> None:
    env_root = tmp_path / ".minds-dev-alice"
    other_root = tmp_path / ".minds-dev-alice-2"
    processes = [
        _process(101, "python", "-m", "imbue.minds.cli", "run", "--config-file", str(env_root / "client.toml")),
        _process(102, "electron", f"--config-file={env_root / 'client.toml'}"),
        _process(103, "python", "-m", "imbue.minds.cli", "run", "--config-file", str(other_root / "client.toml")),
        _process(104, "sleep", "1"),
    ]
    assert desktop_pids_for_env_root(processes, env_root) == (101, 102)
    assert desktop_pids_for_env_root([], env_root) == ()


def test_desktop_pids_match_a_dev_launch_by_its_environment(tmp_path: Path) -> None:
    """A ``just minds-start`` backend carries the config path in its environment, not on its argv.

    The ``mngr`` children it spawns inherit that environment, so only a process
    running the ``minds`` entrypoint counts; another env root's backend does not.
    """
    env_root = tmp_path / ".minds-dev-alice"
    other_root = tmp_path / ".minds-dev-alice-2"
    this_env = {"MINDS_CLIENT_CONFIG_PATH": str(env_root / "client.toml")}
    other_env = {"MINDS_CLIENT_CONFIG_PATH": str(other_root / "client.toml")}
    processes = [
        _process(201, "uv", "run", "--package", "minds", "minds", "-vv", "run", "--port", "41245", environ=this_env),
        _process(
            202, "/repo/.venv/bin/python", "/repo/.venv/bin/minds", "-vv", "run", "--port", "41245", environ=this_env
        ),
        _process(203, "mngr", "forward", "--service", "system_interface", environ=this_env),
        _process(
            204, "mngr", "latchkey", "forward", "--latchkey-directory", str(env_root / "latchkey"), environ=this_env
        ),
        _process(205, "/repo/.venv/bin/python", "/repo/.venv/bin/minds", "run", "--port", "8431", environ=other_env),
        _process(206, "/repo/.venv/bin/python", "/repo/.venv/bin/minds", "run", "--port", "8432"),
    ]
    assert desktop_pids_for_env_root(processes, env_root) == (201, 202)


def test_describe_env_local_holder_lines_names_each_holder() -> None:
    lines = describe_env_local_holder_lines(EnvLocalHolders(latchkey_forward_pid=4242, desktop_pids=(17, 18)))
    assert len(lines) == 3
    assert "pid 17" in lines[0]
    assert "pid 18" in lines[1]
    assert "pid 4242" in lines[2]
    assert describe_env_local_holder_lines(EnvLocalHolders(latchkey_forward_pid=None, desktop_pids=())) == []


def test_find_env_local_holders_reports_a_live_latchkey_forward_and_nothing_once_released(tmp_path: Path) -> None:
    env_root = tmp_path / ".minds-dev-alice"
    lock = acquire_forward_lock(env_latchkey_plugin_data_dir(env_root))
    assert lock is not None

    holders = find_env_local_holders(env_root)
    assert holders.is_anything_running
    assert holders.latchkey_forward_pid is not None
    assert holders.desktop_pids == ()

    # The lock is released with its holder, so a departed supervisor no longer counts.
    del lock
    assert not find_env_local_holders(env_root).is_anything_running


def test_describe_env_local_holders_names_each_holder_and_the_override(tmp_path: Path) -> None:
    env_root = tmp_path / ".minds-dev-alice"
    message = describe_env_local_holders(env_root, EnvLocalHolders(latchkey_forward_pid=4242, desktop_pids=(17,)))
    assert str(env_root) in message
    assert "pid 17" in message
    assert "pid 4242" in message
    assert "--stop-local-processes" in message
    assert "env stop-local" in message


def test_stop_env_local_processes_terminates_the_holders_and_tolerates_gone_pids() -> None:
    # The child blocks until a signal arrives; the unique marker in its argv keeps
    # it distinguishable from any other process on the machine.
    child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()", uuid4().hex])
    try:
        stop_env_local_processes(EnvLocalHolders(latchkey_forward_pid=None, desktop_pids=(child.pid,)))
        child.wait(timeout=5)
        assert child.poll() is not None
        # A pid that already exited is simply skipped.
        stop_env_local_processes(EnvLocalHolders(latchkey_forward_pid=child.pid, desktop_pids=()))
    finally:
        if child.poll() is None:
            child.kill()


@pytest.mark.parametrize("pids", [(), (1234,)])
def test_is_anything_running_reflects_either_holder(pids: tuple[int, ...]) -> None:
    assert EnvLocalHolders(latchkey_forward_pid=None, desktop_pids=pids).is_anything_running is bool(pids)
    assert EnvLocalHolders(latchkey_forward_pid=99, desktop_pids=pids).is_anything_running
