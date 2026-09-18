"""The models a codex ACCOUNT offers, asked of the account itself rather than of one of its agents.

Codex's picker is DYNAMIC: the model set, each model's efforts, and its fast support are all
account-derived (subscription tier), so -- unlike a static catalog -- there is nothing to offer for
an account until a codex signed in AS that account has been asked. The switch dialog needs exactly
that for an account the chat is not on yet: a rebind's destination, or a handoff's.

So the account is asked directly. ``CODEX_HOME`` IS the account folder -- that is the account
binding's own scope (:class:`~imbue.chat.harnesses.codex.account_binding.CodexAccountBinding`) -- so
a ``codex app-server`` launched against it is a fully bound codex. It answers ``model/list`` after
the ``initialize`` handshake alone: that call is account-scoped and binds no thread, so the probe
needs no agent, no tmux session and no conversation.

Measured against codex-cli 0.154.0: the whole exchange takes well under a second, and the two
accounts of one provider genuinely differ (one offered a model and a service tier the other did
not), which is why an agent of a DIFFERENT account is not an acceptable stand-in.

The daemon is spawned into a ConcurrencyGroup owned by this call and torn down before it returns,
so no probe outlives the request that asked.
"""

import os
import threading
import time
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger

from imbue.chat.harnesses.codex.account_binding import CodexAccountBinding
from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ConcurrencyGroupError
from imbue.concurrency_group.local_process import RunningProcess
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import CodexModel
from imbue.mngr_codex.app_server_client import connect_app_server_transport
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path

logger = _loguru_logger

_PROBE_CLIENT_NAME: Final[str] = "minds-chat-account-models"
_PROBE_CLIENT_VERSION: Final[str] = "1"
# The daemon binds its socket in well under a second; the ceiling is for a loaded host.
_SOCKET_WAIT_SECONDS: Final[float] = 15.0
_SOCKET_POLL_INTERVAL_SECONDS: Final[float] = 0.05
# A ceiling on the whole probe, so a wedged daemon cannot hold the dialog's request open.
_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0
# Layered over the server's own environment, never used alone: the child needs PATH to find codex.
# But the server's environment is not neutral -- a workspace can carry an ambient OPENAI_API_KEY,
# and a codex that reads one answers for THAT key rather than for the folder we are asking about,
# which is the one thing this probe must never do. Same reasoning as the signed-in probe's.
_AMBIENT_AUTH_KEYS: Final[frozenset[str]] = frozenset(("OPENAI_API_KEY",))
# One probe of a given account at a time. The socket path is a hash of the account folder, so every
# probe of one account wants the same one: overlapping probes (two switch dialogs opened on the same
# account, each served by its own thread of the threaded WSGI server) would unlink each other's live
# socket and launch two daemons on one listen address, leaving both pickers waiting on a path that no
# longer exists. Keyed by the resolved folder, and never evicted -- an account is probed by the one
# server process, and there are a handful of them.
_PROBE_LOCKS_BY_ACCOUNT_DIR: Final[dict[Path, threading.Lock]] = {}
_PROBE_LOCKS_GUARD: Final[threading.Lock] = threading.Lock()


class AccountModelProbeError(RuntimeError):
    """The account's models could not be read: codex would not start, bind, or answer."""


def _probe_lock(account_dir: Path) -> threading.Lock:
    """The lock that admits one probe of ``account_dir`` at a time."""
    with _PROBE_LOCKS_GUARD:
        return _PROBE_LOCKS_BY_ACCOUNT_DIR.setdefault(account_dir.resolve(), threading.Lock())


def _probe_environment(account_dir: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in _AMBIENT_AUTH_KEYS}
    environment.update(CodexAccountBinding().account_env(account_dir))
    return environment


def _wait_for_socket(socket_path: Path, process: RunningProcess, deadline: float) -> None:
    """Block until the daemon has bound ``socket_path``, or raise when it dies without binding.

    Polled rather than awaited: codex prints nothing when it binds (verified against 0.154.0 --
    its only startup line is an unrelated sandbox warning), so there is no readiness signal to
    block on. mngr's own launcher waits the same way.

    The child is watched alongside the socket because a codex that starts and exits -- signed out,
    crashed, a build with no ``app-server`` -- is the common failure, and waiting the full ceiling
    out on it would hold the switch dialog's request open for fifteen seconds and then report a
    bind timeout, hiding the reason codex itself printed.
    """
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        if process.is_finished():
            stderr = process.read_stderr().strip() or "no output"
            raise AccountModelProbeError(
                f"codex app-server exited (code {process.returncode}) without binding {socket_path}: {stderr}"
            )
        time.sleep(_SOCKET_POLL_INTERVAL_SECONDS)
    raise AccountModelProbeError(f"codex app-server did not bind {socket_path} within {_SOCKET_WAIT_SECONDS:.0f}s")


def probe_codex_account_models(account_dir: Path) -> tuple[CodexModel, ...]:
    """The raw ``model/list`` the account's own codex answers, or raise :class:`AccountModelProbeError`.

    Raw entries, not mapped options: the caller persists exactly what the daemon said, so a later
    change to the mapping needs no second probe.

    One probe of an account at a time; a concurrent caller waits its turn and then runs its own.
    """
    with _probe_lock(account_dir):
        return _probe_bound_codex(account_dir)


def _remove_socket(socket_path: Path) -> None:
    """Drop the account's socket path, warning rather than raising when it will not go.

    Best effort at both ends of a probe: a socket a killed probe left behind is otherwise taken for
    a live daemon, and one this probe bound would otherwise outlive it. Neither is worth failing the
    caller over, and raising here would escape the translation below -- an ``OSError`` from the
    removal is not an answer about the account's models, and the picker's sidecar fallback (which
    only catches :class:`AccountModelProbeError`) would be skipped for it.
    """
    try:
        socket_path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Could not remove the codex account probe socket {}: {}", socket_path, e)


def _probe_bound_codex(account_dir: Path) -> tuple[CodexModel, ...]:
    """Launch a codex bound to ``account_dir``, ask it for its models, and tear it down."""
    socket_path = get_codex_app_server_socket_path(account_dir)
    _remove_socket(socket_path)
    command = ["codex", "app-server", "--listen", f"unix://{socket_path}"]
    deadline = time.monotonic() + _SOCKET_WAIT_SECONDS
    try:
        with ConcurrencyGroup(name="codex-account-model-options") as group:
            # Unchecked and explicitly terminated: the probe ends by killing a daemon that would
            # otherwise run forever, and SIGTERM is not a failure to report.
            process = group.run_process_in_background(
                command=command,
                env=_probe_environment(account_dir),
                is_checked_by_group=False,
                timeout=_PROBE_TIMEOUT_SECONDS,
                name="codex account model-options probe",
            )
            try:
                _wait_for_socket(socket_path, process, deadline)
                client = CodexAppServerClient(transport=connect_app_server_transport(socket_path))
                try:
                    client.initialize(_PROBE_CLIENT_NAME, _PROBE_CLIENT_VERSION)
                    return client.model_list()
                finally:
                    client.close()
            finally:
                process.terminate()
    # Whatever fails inside the group reaches the caller only when the group exits, re-raised as
    # its ExceptionGroup rather than as itself -- including a missing codex, whose spawn failure
    # ``run_process_in_background`` raises right there in the body.
    except (CodexAppServerError, ConcurrencyExceptionGroup, ConcurrencyGroupError, OSError) as e:
        raise AccountModelProbeError(f"could not read the models of the codex account at {account_dir}: {e}") from e
    finally:
        _remove_socket(socket_path)
