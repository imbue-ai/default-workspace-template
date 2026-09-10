"""The local processes still holding an env root, checked before ``minds-admin env destroy`` removes anything."""

import signal
from collections.abc import Sequence
from pathlib import Path

import psutil
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.mngr_latchkey.store import probe_forward_lock

# The desktop's default layout under the env root (``minds run`` without a
# MINDS_LATCHKEY_DIRECTORY override): upstream latchkey's store, and the
# plugin's own subdirectory holding the forward lock.
_LATCHKEY_SUBDIR: str = "latchkey"
_LATCHKEY_PLUGIN_SUBDIR: str = "mngr_latchkey"
_CLIENT_CONFIG_FILENAME: str = "client.toml"

_STOP_TIMEOUT_SECONDS: float = 30.0


class EnvLocalProcessesStillRunningError(MindError):
    """Raised when local processes holding the env root did not exit after being asked to stop."""


class EnvLocalHolders(FrozenModel):
    """The processes on this machine that still hold an env root open."""

    latchkey_forward_pid: int | None = Field(
        description="The `mngr latchkey forward` supervisor owning the env's latchkey directory, if one is live"
    )
    desktop_pids: tuple[int, ...] = Field(description="minds desktop backends launched with the env's client.toml")

    @property
    def is_anything_running(self) -> bool:
        return self.latchkey_forward_pid is not None or bool(self.desktop_pids)


@pure
def env_latchkey_plugin_data_dir(env_root: Path) -> Path:
    return env_root / _LATCHKEY_SUBDIR / _LATCHKEY_PLUGIN_SUBDIR


@pure
def desktop_pids_for_env_root(processes: Sequence[tuple[int, Sequence[str]]], env_root: Path) -> tuple[int, ...]:
    """The pids whose command line names the env's client.toml (how the desktop backend is launched)."""
    client_config = str(env_root / _CLIENT_CONFIG_FILENAME)
    return tuple(pid for pid, argv in processes if any(client_config in argument for argument in argv))


def list_running_processes() -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Every process this user can see, as (pid, argv); processes that vanish mid-scan are skipped."""
    processes: list[tuple[int, tuple[str, ...]]] = []
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            argv = process.info["cmdline"] or ()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        processes.append((process.info["pid"], tuple(argv)))
    return tuple(processes)


def find_env_local_holders(env_root: Path) -> EnvLocalHolders:
    """What still holds ``env_root``: the latchkey supervisor (by its lock) and desktop backends (by argv)."""
    owner = probe_forward_lock(env_latchkey_plugin_data_dir(env_root))
    return EnvLocalHolders(
        latchkey_forward_pid=owner.pid if owner is not None else None,
        desktop_pids=desktop_pids_for_env_root(list_running_processes(), env_root),
    )


@pure
def describe_env_local_holders(env_root: Path, holders: EnvLocalHolders) -> str:
    lines = [f"Local processes still hold {env_root}; destroying the env under them would strand them:"]
    for pid in holders.desktop_pids:
        lines.append(f"  - minds desktop backend (pid {pid}); stop it with `just minds-stop` or `kill {pid}`")
    if holders.latchkey_forward_pid is not None:
        lines.append(
            f"  - `mngr latchkey forward` supervisor (pid {holders.latchkey_forward_pid}); SIGTERM runs its "
            "teardown (gateway, `mngr observe`, tunnels)"
        )
    lines.append("Stop them and re-run, or pass --stop-local-processes to have the destroy SIGTERM them first.")
    return "\n".join(lines)


def stop_env_local_processes(holders: EnvLocalHolders) -> None:
    """SIGTERM the desktop backends, then the latchkey supervisor, and wait for every one to exit."""
    pids = [*holders.desktop_pids]
    if holders.latchkey_forward_pid is not None:
        pids.append(holders.latchkey_forward_pid)
    processes: list[psutil.Process] = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue
        logger.info("Stopping local process {} ({}) before destroying the env", pid, " ".join(process.cmdline()[:3]))
        process.send_signal(signal.SIGTERM)
        processes.append(process)
    _gone, alive = psutil.wait_procs(processes, timeout=_STOP_TIMEOUT_SECONDS)
    if alive:
        raise EnvLocalProcessesStillRunningError(
            "these processes did not exit within {:.0f}s of SIGTERM: {}".format(
                _STOP_TIMEOUT_SECONDS, ", ".join(str(process.pid) for process in alive)
            )
        )
