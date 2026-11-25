"""Drive the Screen's browser from a script, with a ``playwright-cli`` session that ends with the block::

    with driven_browser(purpose="plaud-sync") as browser:
        browser.run("goto", "https://example.com")
        title = browser.run("eval", "() => document.title", "--raw")

The block reserves the browser (the Screen's lease), attaches a session under a name no other caller uses, and when
the body finishes or raises, detaches that session and gives the lease back. The browser keeps running. A process
killed by a signal (a routine's time limit sends SIGTERM, then SIGKILL) never reaches that cleanup, and the session's
daemon, spawned detached, outlives it: only the service's reaper (``screen.cli_sessions``) ends that one.
"""

import shutil
import subprocess
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from screen.browser_names import MAX_NAME_LENGTH
from screen.browser_names import NAME_PATTERN
from screen.cli import ServiceAnswer
from screen.cli import call_service
from screen.cli import identity_headers
from screen.errors import BrowserSessionError
from screen.errors import BrowserUnavailableError
from screen.errors import InvalidSessionPurposeError
from screen.errors import ScreenServiceUnreachableError
from screen.fleet_cli import refusal_reason
from screen.identity import AGENT_ID_HEADER
from screen.primitives import PLAYWRIGHT_CLI_COMMAND

DEFAULT_BROWSER_NAME: Final[str] = "browser-1"
DEFAULT_COMMAND_TIMEOUT_SECONDS: Final[float] = 300.0
_ATTACH_TIMEOUT_SECONDS: Final[float] = 120.0
_DETACH_TIMEOUT_SECONDS: Final[float] = 60.0


class DrivenBrowser(FrozenModel):
    """The attached session a ``driven_browser`` block hands its body."""

    browser_name: str = Field(description="The browser the session drives")
    session_name: str = Field(description="The session's ``-s=`` name, unique to this block")
    playwright_cli: str = Field(description="The ``playwright-cli`` executable")

    def run(self, command: str, *args: str, timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> str:
        """Run one ``playwright-cli`` command in the session and answer its output; raises
        ``BrowserSessionError`` when it fails (the CLI exits 1 for any tool error)."""
        return _run_cli(self.playwright_cli, self.session_name, command, args, timeout_seconds)


@contextmanager
def driven_browser(
    browser_name: str = DEFAULT_BROWSER_NAME, purpose: str = "script", is_reclaim: bool = False
) -> Generator[DrivenBrowser]:
    """Reserve ``browser_name``, attach a session named after ``purpose`` for the block, then detach and release.

    Raises ``BrowserUnavailableError`` (with the service's ``status``: ``busy_human``, ``busy_agent``, ``starting``,
    ``stopped`` or ``crashed``, or the HTTP status, ``404`` for a browser that does not exist) when the browser cannot
    be reserved; nothing is attached then, but a ``busy_*`` refusal queues the run's agent, as ``acquire`` does, so it
    is handed the Screen and messaged once it frees. A Screen service that does not answer raises
    ``ScreenServiceUnreachableError``. ``is_reclaim`` takes the Screen back from the human, only for a run the user
    started themselves. A detach that fails after the body succeeded raises ``BrowserSessionError``; after a body that
    raised, it is logged and the body's error goes on. ``purpose`` is lowercase letters and digits in words joined by
    single dashes, at most 40 characters; any other raises ``InvalidSessionPurposeError`` before anything is reserved.
    """
    # The reaper cannot recognize a daemon whose session name starts with a dash, so it would never end one.
    if len(purpose) > MAX_NAME_LENGTH or NAME_PATTERN.fullmatch(purpose) is None:
        raise InvalidSessionPurposeError(
            f"invalid purpose {purpose!r}: lowercase letters, digits and single dashes, at most {MAX_NAME_LENGTH}"
        )
    playwright_cli = shutil.which(PLAYWRIGHT_CLI_COMMAND)
    if playwright_cli is None:
        raise BrowserSessionError(f"{PLAYWRIGHT_CLI_COMMAND} is not installed")
    headers = identity_headers()
    if AGENT_ID_HEADER not in headers:
        raise BrowserSessionError("MNGR_AGENT_ID is not set: run the script from inside an agent")
    acquired = call_service("POST", f"/browsers/{browser_name}/acquire", {"reclaim": is_reclaim}, headers)
    if not acquired.body.get("ok"):
        raise _unavailable(acquired, browser_name, "acquire")
    session_name = f"{purpose}-{uuid4().hex[:12]}"
    is_body_done = False
    try:
        attach = call_service("GET", f"/browsers/{browser_name}/attach", None, headers)
        attach_url = attach.body.get("attach_url")
        if not attach.body.get("ok") or not isinstance(attach_url, str):
            raise _unavailable(attach, browser_name, "attach")
        _run_cli(playwright_cli, session_name, "attach", [f"--cdp={attach_url}"], _ATTACH_TIMEOUT_SECONDS)
        yield DrivenBrowser(browser_name=browser_name, session_name=session_name, playwright_cli=playwright_cli)
        is_body_done = True
    finally:
        detach_error: BrowserSessionError | None = None
        try:
            # A session the attach never started detaches cleanly.
            _run_cli(playwright_cli, session_name, "detach", [], _DETACH_TIMEOUT_SECONDS)
        except BrowserSessionError as e:
            detach_error = BrowserSessionError(f"could not detach the playwright-cli session {session_name}: {e}")
        _release(browser_name, headers)
        if detach_error is not None:
            if is_body_done:
                raise detach_error
            logger.warning("{}", detach_error)


def _release(browser_name: str, headers: dict[str, str]) -> None:
    """Give the lease back; a failure is logged, since the lease also expires on its own when unused."""
    try:
        released = call_service("POST", f"/browsers/{browser_name}/release", {}, headers)
    except ScreenServiceUnreachableError as e:
        logger.warning("Releasing browser {} did not reach the Screen service: {}", browser_name, e)
        return
    if released.status != 200:
        logger.warning("Releasing browser {} answered {}: {}", browser_name, released.status, released.body)


def _unavailable(answer: ServiceAnswer, browser_name: str, verb: str) -> BrowserUnavailableError:
    status = str(answer.body.get("status") or answer.status)
    fallback = f"browser {browser_name} could not be reserved ({verb}: {status})"
    detail = str(answer.body.get("hint") or refusal_reason(answer.body, fallback))
    return BrowserUnavailableError(status, detail)


def _run_cli(playwright_cli: str, session_name: str, command: str, args: Sequence[str], timeout_seconds: float) -> str:
    argv = [playwright_cli, f"-s={session_name}", command, *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as e:
        raise BrowserSessionError(f"playwright-cli {command} did not finish within {timeout_seconds:.0f}s") from e
    except OSError as e:
        raise BrowserSessionError(f"playwright-cli could not run ({e})") from e
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise BrowserSessionError(f"playwright-cli {command} failed: {output[-400:]}")
    return result.stdout
