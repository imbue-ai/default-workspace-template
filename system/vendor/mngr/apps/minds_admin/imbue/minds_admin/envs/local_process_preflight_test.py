import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.minds_admin.envs.local_process_preflight import EnvLocalHolders
from imbue.minds_admin.envs.local_process_preflight import describe_env_local_holders
from imbue.minds_admin.envs.local_process_preflight import desktop_pids_for_env_root
from imbue.minds_admin.envs.local_process_preflight import env_latchkey_plugin_data_dir
from imbue.minds_admin.envs.local_process_preflight import find_env_local_holders
from imbue.minds_admin.envs.local_process_preflight import stop_env_local_processes
from imbue.mngr_latchkey.store import acquire_forward_lock


def test_desktop_pids_match_only_processes_launched_with_this_envs_client_toml(tmp_path: Path) -> None:
    env_root = tmp_path / ".minds-dev-alice"
    other_root = tmp_path / ".minds-dev-alice-2"
    processes = [
        (101, ("python", "-m", "imbue.minds.cli", "run", "--config-file", str(env_root / "client.toml"))),
        (102, ("electron", f"--config-file={env_root / 'client.toml'}")),
        (103, ("python", "-m", "imbue.minds.cli", "run", "--config-file", str(other_root / "client.toml"))),
        (104, ("sleep", "1")),
    ]
    assert desktop_pids_for_env_root(processes, env_root) == (101, 102)
    assert desktop_pids_for_env_root([], env_root) == ()


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
