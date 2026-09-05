import subprocess
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from terminal_app.data_types import TmuxClient, TmuxSession
from terminal_app.errors import InvalidTerminalValueError, TmuxCommandError
from terminal_app.interfaces import TmuxInterface
from terminal_app.primitives import ClientTty, TmuxSessionName

# A tmux command is one round trip to a local socket; past the first threshold it is suspicious,
# past the second it is broken.
TMUX_SLOW_SECONDS: Final[float] = 1.0
TMUX_TIMEOUT_SECONDS: Final[float] = 5.0

SESSIONS_FORMAT: Final[str] = "#{session_name}\t#{session_id}\t#{session_activity}"
CLIENTS_FORMAT: Final[str] = "#{client_tty}\t#{session_name}\t#{session_id}"


@pure
def parse_tmux_sessions(output: str) -> list[TmuxSession]:
    """Parse ``tmux list-sessions`` lines of ``name\\tid\\tactivity``; a line with fewer fields is skipped."""
    sessions: list[TmuxSession] = []
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) < 3:
            continue
        name, session_id, raw_activity = fields
        sessions.append(
            TmuxSession(
                name=name,
                session_id=session_id,
                last_activity=_parse_activity(raw_activity),
            )
        )
    return sessions


@pure
def _parse_activity(raw_activity: str) -> datetime | None:
    """``#{session_activity}`` is a Unix timestamp in seconds; anything else reads as unknown."""
    if not raw_activity.isdigit():
        return None
    return datetime.fromtimestamp(int(raw_activity), timezone.utc)


@pure
def parse_tmux_clients(output: str) -> list[TmuxClient]:
    """Parse ``tmux list-clients`` lines of ``tty\\tname\\tid``; a line with fewer fields, or whose tty is not a device path, is skipped."""
    clients: list[TmuxClient] = []
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) < 3:
            continue
        client_tty, session_name, session_id = fields
        if not _is_client_tty(client_tty):
            continue
        clients.append(
            TmuxClient(
                client_tty=ClientTty(client_tty),
                session_name=session_name,
                session_id=session_id,
            )
        )
    return clients


@pure
def _is_client_tty(value: str) -> bool:
    try:
        ClientTty(value)
    except InvalidTerminalValueError:
        return False
    return True


class SubprocessTmux(TmuxInterface):
    """Drives the default tmux server through the ``tmux`` binary on PATH."""

    tmux_executable: str = Field(
        default="tmux", frozen=True, description="The tmux binary to run"
    )

    def list_sessions(self) -> list[TmuxSession]:
        # A missing server (no sessions yet) is a non-zero exit, not an error.
        completed = self._run(["list-sessions", "-F", SESSIONS_FORMAT])
        if completed.returncode != 0:
            logger.debug("Listed no tmux sessions: {}", completed.stderr.strip())
            return []
        return parse_tmux_sessions(completed.stdout)

    def list_clients(self) -> list[TmuxClient]:
        completed = self._run(["list-clients", "-F", CLIENTS_FORMAT])
        if completed.returncode != 0:
            logger.debug("Listed no tmux clients: {}", completed.stderr.strip())
            return []
        return parse_tmux_clients(completed.stdout)

    def kill_session(self, name: TmuxSessionName) -> None:
        # ``=`` forces an exact match so tmux's prefix fallback cannot target another session.
        # tmux exits non-zero both for a real failure and for an already-absent session, so
        # the two are told apart by re-listing.
        completed = self._run(["kill-session", "-t", f"={name}"])
        if completed.returncode == 0:
            return
        if any(session.name == name for session in self.list_sessions()):
            raise TmuxCommandError(
                f"tmux could not kill session {name!r}: {completed.stderr.strip()}"
            )

    def rename_session(self, name: TmuxSessionName, new_name: TmuxSessionName) -> None:
        completed = self._run(["rename-session", "-t", f"={name}", new_name])
        if completed.returncode != 0:
            raise TmuxCommandError(
                f"tmux could not rename session {name!r} to {new_name!r}: {completed.stderr.strip()}"
            )

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = [self.tmux_executable, *arguments]
        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=TMUX_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as e:
            raise TmuxCommandError(
                f"tmux did not finish {arguments[0]} within {TMUX_TIMEOUT_SECONDS}s"
            ) from e
        except OSError as e:
            raise TmuxCommandError(f"cannot run {self.tmux_executable}: {e}") from e
        elapsed = time.monotonic() - started_at
        if elapsed > TMUX_SLOW_SECONDS:
            logger.warning("Ran tmux {} slowly, in {:.1f}s", arguments[0], elapsed)
        return completed
