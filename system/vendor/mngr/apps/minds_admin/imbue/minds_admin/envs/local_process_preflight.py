"""The local processes still holding an env root: what ``minds-admin env destroy`` refuses over, and ``env stop-local`` stops."""

import signal
from collections.abc import Mapping
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
# How the backend learns its config when no ``--config-file`` is passed: the
# packaged Electron build passes the flag, a ``just minds-start`` dev launch
# inherits the variable from ``minds-admin env activate``.
_CLIENT_CONFIG_ENV_VAR: str = "MINDS_CLIENT_CONFIG_PATH"
# The console script every desktop backend runs as (bare on ``uv run``'s argv,
# a venv path on the interpreter's), which tells the backend apart from the
# ``mngr`` children that inherit its environment.
_MINDS_ENTRYPOINT_NAME: str = "minds"

_STOP_TIMEOUT_SECONDS: float = 30.0


class EnvLocalProcessesStillRunningError(MindError):
    """Raised when local processes holding the env root did not exit after being asked to stop."""


class EnvLocalHolders(FrozenModel):
    """The processes on this machine that still hold an env root open."""

    latchkey_forward_pid: int | None = Field(
        description="The `mngr latchkey forward` supervisor owning the env's latchkey directory, if one is live"
    )
    desktop_pids: tuple[int, ...] = Field(
        description="minds desktop backends running against the env's client.toml (on their argv or in their environment)"
    )

    @property
    def is_anything_running(self) -> bool:
        return self.latchkey_forward_pid is not None or bool(self.desktop_pids)


@pure
def env_latchkey_plugin_data_dir(env_root: Path) -> Path:
    return env_root / _LATCHKEY_SUBDIR / _LATCHKEY_PLUGIN_SUBDIR


class RunningProcess(FrozenModel):
    """One process on this machine, as much of it as the preflight can read."""

    pid: int = Field(description="Process id")
    argv: tuple[str, ...] = Field(description="Command line; empty when unreadable")
    environ: Mapping[str, str] = Field(
        default_factory=dict, description="Environment; empty when unreadable (another user's process)"
    )


@pure
def _is_minds_entrypoint(argv: Sequence[str]) -> bool:
    return any(Path(argument).name == _MINDS_ENTRYPOINT_NAME for argument in argv)


@pure
def desktop_pids_for_env_root(processes: Sequence[RunningProcess], env_root: Path) -> tuple[int, ...]:
    """The pids of desktop backends launched against this env root.

    A packaged build names the env's ``client.toml`` on its command line; a
    dev launch carries it in ``MINDS_CLIENT_CONFIG_PATH`` instead, and so do
    the ``mngr`` children the backend spawns, which is why the environment
    match also requires the ``minds`` entrypoint on the command line.
    """
    client_config = env_root / _CLIENT_CONFIG_FILENAME
    client_config_str = str(client_config)
    matching: list[int] = []
    for process in processes:
        is_named_on_argv = any(client_config_str in argument for argument in process.argv)
        config_from_environ = process.environ.get(_CLIENT_CONFIG_ENV_VAR)
        is_named_in_environ = (
            config_from_environ is not None
            and Path(config_from_environ).expanduser() == client_config
            and _is_minds_entrypoint(process.argv)
        )
        if is_named_on_argv or is_named_in_environ:
            matching.append(process.pid)
    return tuple(matching)


def list_running_processes() -> tuple[RunningProcess, ...]:
    """Every process this user can see; a process that vanishes mid-scan is skipped, an unreadable field reads empty."""
    processes: list[RunningProcess] = []
    for process in psutil.process_iter(["pid", "cmdline", "environ"], ad_value=None):
        try:
            argv = process.info["cmdline"] or ()
            environ = process.info["environ"] or {}
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        processes.append(RunningProcess(pid=process.info["pid"], argv=tuple(argv), environ=dict(environ)))
    return tuple(processes)


def find_env_local_holders(env_root: Path) -> EnvLocalHolders:
    """What still holds ``env_root``: the latchkey supervisor (by its lock) and desktop backends (by their config path)."""
    owner = probe_forward_lock(env_latchkey_plugin_data_dir(env_root))
    return EnvLocalHolders(
        latchkey_forward_pid=owner.pid if owner is not None else None,
        desktop_pids=desktop_pids_for_env_root(list_running_processes(), env_root),
    )


@pure
def describe_env_local_holder_lines(holders: EnvLocalHolders) -> list[str]:
    """One line naming each holder."""
    lines = [f"  - minds desktop backend (pid {pid})" for pid in holders.desktop_pids]
    if holders.latchkey_forward_pid is not None:
        lines.append(f"  - `mngr latchkey forward` supervisor (pid {holders.latchkey_forward_pid})")
    return lines


@pure
def describe_env_local_holders(env_root: Path, holders: EnvLocalHolders) -> str:
    """The destroy refusal: the holders, and how to clear them."""
    lines = [f"Local processes still hold {env_root}; destroying the env under them would strand them:"]
    lines.extend(describe_env_local_holder_lines(holders))
    lines.append(
        "Stop them with `minds-admin env stop-local <env>` and re-run, or pass --stop-local-processes to have "
        "the destroy SIGTERM them first (the supervisor's SIGTERM runs its teardown: gateway, `mngr observe`, "
        "tunnels)."
    )
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
        logger.info("Stopping local process {} ({}) holding the env root", pid, " ".join(process.cmdline()[:3]))
        process.send_signal(signal.SIGTERM)
        processes.append(process)
    _gone, alive = psutil.wait_procs(processes, timeout=_STOP_TIMEOUT_SECONDS)
    if alive:
        raise EnvLocalProcessesStillRunningError(
            "these processes did not exit within {:.0f}s of SIGTERM: {}".format(
                _STOP_TIMEOUT_SECONDS, ", ".join(str(process.pid) for process in alive)
            )
        )
