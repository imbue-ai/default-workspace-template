"""Ending the ``playwright-cli`` sessions left attached to the Screen's browser.

``playwright-cli attach`` starts a daemon (``cliDaemon.js``) that holds the session's CDP connection through the proxy,
and the CLI spawns it detached, so it outlives the command that started it. Only ``detach`` ends it. A script that is
killed, times out, or never detaches leaves its daemon behind for good, each holding a few hundred MB.

The service's reap loop ends a session once nothing can use it any more: its token is no longer the browser's (every
attach mints a new token, and the proxy refuses frames bearing any other, so a superseded session can never drive
again), or its browser is gone, or it has sent no frame for ``IDLE_LIMIT_SECONDS`` while its agent does not hold the
lease. A session is ended the way ``playwright-cli detach`` ends it: the same command, run with the daemon's own working
directory and environment so the CLI finds the daemon's entry in its session registry. The daemon then deletes its
entry and exits, and the browser keeps running. A daemon that is still there after that is sent SIGTERM.
"""

import os
import shutil
import signal
import subprocess
import threading
import time
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.browser_fleet import Browser
from screen.browser_fleet import BrowserFleet
from screen.cdp_proxy import ProxyServer
from screen.data_types import Controller
from screen.primitives import AgentId
from screen.primitives import PLAYWRIGHT_CLI_COMMAND

# Long enough that an agent driving by hand keeps its session between steps of a long turn.
IDLE_LIMIT_SECONDS: Final[float] = 30 * 60

_CLI_DAEMON_SCRIPT: Final[str] = "cliDaemon.js"
_CDP_FLAG: Final[str] = "--cdp="
_PROC_DIR: Final[Path] = Path("/proc")
_DETACH_TIMEOUT_SECONDS: Final[float] = 30.0
_EXIT_WAIT_SECONDS: Final[float] = 5.0
_EXIT_POLL_SECONDS: Final[float] = 0.1


class AbandonReason(UpperCaseStrEnum):
    """Why a session can be ended."""

    SUPERSEDED = auto()
    BROWSER_GONE = auto()
    IDLE = auto()


class AttachedCliSession(FrozenModel):
    """A running ``playwright-cli`` daemon attached through the Screen's CDP proxy."""

    pid: int = Field(description="The daemon's process")
    session_name: str = Field(description="The ``-s=`` name the session was attached under")
    browser_name: str = Field(description="The browser its attach URL names")
    token: str = Field(description="The capability token its attach URL carries")
    working_dir: Path | None = Field(description="The daemon's working directory, None when it cannot be read")
    environment: dict[str, str] = Field(description="The daemon's environment, empty when it cannot be read")


@pure
def parse_cli_daemon_argv(argv: Sequence[str]) -> tuple[str, str] | None:
    """The session name and the ``--cdp`` URL of a ``cliDaemon.js`` command line, or None for any other process."""
    script_index = next((i for i, arg in enumerate(argv) if arg.endswith(_CLI_DAEMON_SCRIPT)), None)
    if script_index is None or script_index + 1 >= len(argv):
        return None
    session_name = argv[script_index + 1]
    cdp_url = next((arg[len(_CDP_FLAG) :] for arg in argv[script_index + 2 :] if arg.startswith(_CDP_FLAG)), None)
    if session_name.startswith("-") or cdp_url is None:
        return None
    return session_name, cdp_url


@pure
def abandonment_reason(
    session: AttachedCliSession,
    browser: Browser | None,
    lease_holder: AgentId | None,
    now: float,
    idle_limit_seconds: float,
) -> AbandonReason | None:
    """Why ``session`` can be ended, or None while it may still drive."""
    if browser is None or not browser.is_running:
        return AbandonReason.BROWSER_GONE
    if session.token != browser.token:
        return AbandonReason.SUPERSEDED
    is_holding_lease = lease_holder is not None and lease_holder == browser.token_owner
    if not is_holding_lease and now - browser.token_used_at >= idle_limit_seconds:
        return AbandonReason.IDLE
    return None


class CliSessionsInterface(MutableModel, ABC):
    """Finds the daemons attached through the proxy, and ends one."""

    @abstractmethod
    def attached_through(self, proxy_server: ProxyServer) -> list[AttachedCliSession]:
        """Every running daemon whose attach URL is one ``proxy_server`` issued."""

    @abstractmethod
    def end(self, session: AttachedCliSession) -> bool:
        """End the daemon; answers whether it is gone."""


class ProcCliSessions(CliSessionsInterface):
    """The daemons as ``/proc`` shows them, ended through ``playwright-cli detach``."""

    proc_dir: Path = Field(default=_PROC_DIR, frozen=True, description="Where the processes are read")
    playwright_cli: str | None = Field(
        default_factory=lambda: shutil.which(PLAYWRIGHT_CLI_COMMAND),
        frozen=True,
        description="The CLI that detaches a session; None where it is not installed (SIGTERM is all that is left)",
    )

    def attached_through(self, proxy_server: ProxyServer) -> list[AttachedCliSession]:
        try:
            entries = [entry for entry in self.proc_dir.iterdir() if entry.name.isdigit()]
        except OSError as e:
            logger.trace("No processes to read at {} ({})", self.proc_dir, e)
            return []
        sessions: list[AttachedCliSession] = []
        for entry in entries:
            session = self._read_session(entry, proxy_server)
            if session is not None:
                sessions.append(session)
        return sessions

    def _read_session(self, entry: Path, proxy_server: ProxyServer) -> AttachedCliSession | None:
        argv = _read_argv(entry)
        parsed = parse_cli_daemon_argv(argv) if argv else None
        if parsed is None:
            return None
        session_name, cdp_url = parsed
        target = proxy_server.parse_attach_url(cdp_url)
        if target is None:
            return None
        try:
            working_dir: Path | None = Path(os.readlink(entry / "cwd"))
        except OSError as e:
            logger.trace("The working directory of pid {} is unreadable ({})", entry.name, e)
            working_dir = None
        return AttachedCliSession(
            pid=int(entry.name),
            session_name=session_name,
            browser_name=target[0],
            token=target[1],
            working_dir=working_dir,
            environment=_read_environment(entry),
        )

    def end(self, session: AttachedCliSession) -> bool:
        if self.playwright_cli is not None and session.working_dir is not None:
            try:
                result = subprocess.run(
                    [self.playwright_cli, f"-s={session.session_name}", "detach"],
                    cwd=session.working_dir,
                    env=session.environment or None,
                    capture_output=True,
                    text=True,
                    timeout=_DETACH_TIMEOUT_SECONDS,
                    check=False,
                )
                if result.returncode != 0:
                    logger.debug(
                        "playwright-cli detach of {} answered {}: {}",
                        session.session_name,
                        result.returncode,
                        (result.stdout + result.stderr).strip()[-300:],
                    )
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.debug("playwright-cli detach of {} did not run ({})", session.session_name, e)
            if self._wait_until_gone(session):
                return True
        if not self._is_daemon_of(session):
            return True
        logger.info("The playwright-cli daemon of {} outlived detach; sending SIGTERM", session.session_name)
        _signal(session.pid, signal.SIGTERM)
        return self._wait_until_gone(session)

    def _is_daemon_of(self, session: AttachedCliSession) -> bool:
        """Whether the pid is still this session's daemon (not gone, and not reused by another process)."""
        argv = _read_argv(self.proc_dir / str(session.pid))
        parsed = parse_cli_daemon_argv(argv) if argv else None
        return parsed is not None and parsed[0] == session.session_name and parsed[1].endswith(f"/{session.token}")

    def _wait_until_gone(self, session: AttachedCliSession) -> bool:
        deadline = time.monotonic() + _EXIT_WAIT_SECONDS
        pause = threading.Event()
        while time.monotonic() < deadline:
            if not self._is_daemon_of(session):
                return True
            pause.wait(_EXIT_POLL_SECONDS)
        return not self._is_daemon_of(session)


class CliSessionReaper(MutableModel):
    """Ends the sessions nothing can use any more; the service runs ``sweep`` on its reap loop."""

    model_config = {"arbitrary_types_allowed": True}

    fleet: BrowserFleet = Field(frozen=True, description="The browsers, their tokens and the lease")
    sessions: CliSessionsInterface = Field(frozen=True, description="Where the daemons are found and ended")
    idle_limit_seconds: float = Field(default=IDLE_LIMIT_SECONDS, frozen=True, description="See IDLE_LIMIT_SECONDS")

    def sweep(self) -> list[tuple[AttachedCliSession, AbandonReason]]:
        """End every abandoned session; answers the ones ended and why."""
        # Read before the tokens, so a token minted meanwhile can only make a found session superseded, never live.
        found = self.sessions.attached_through(self.fleet.proxy_server)
        if not found:
            return []
        view = self.fleet.control.view()
        lease_holder = view.agent_id if view.controller == Controller.AGENT else None
        now = self.fleet.clock()
        browsers = {str(browser.name): browser for browser in self.fleet.browsers()}
        ended: list[tuple[AttachedCliSession, AbandonReason]] = []
        for session in found:
            reason = abandonment_reason(
                session, browsers.get(session.browser_name), lease_holder, now, self.idle_limit_seconds
            )
            if reason is None:
                continue
            if self.sessions.end(session):
                logger.info(
                    "Ended the playwright-cli session {} (pid {}) on browser {}: {}",
                    session.session_name,
                    session.pid,
                    session.browser_name,
                    reason.value.lower(),
                )
                ended.append((session, reason))
            else:
                logger.warning(
                    "Could not end the playwright-cli session {} (pid {})", session.session_name, session.pid
                )
        return ended


def _read_argv(entry: Path) -> list[str]:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError as e:
        logger.trace("The command line of pid {} is unreadable ({})", entry.name, e)
        return []
    return [arg.decode(errors="replace") for arg in raw.split(b"\0") if arg]


def _read_environment(entry: Path) -> dict[str, str]:
    try:
        raw = (entry / "environ").read_bytes()
    except OSError as e:
        logger.trace("The environment of pid {} is unreadable ({})", entry.name, e)
        return {}
    pairs = (item.decode(errors="replace").partition("=") for item in raw.split(b"\0") if item)
    return {key: value for key, separator, value in pairs if separator}


def _signal(pid: int, signal_number: signal.Signals) -> None:
    try:
        os.kill(pid, signal_number)
    except OSError as e:
        logger.debug("Signalling pid {} was ignored ({})", pid, e)
