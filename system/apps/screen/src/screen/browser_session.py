"""Drive the Screen's browser from a script, with a ``playwright-cli`` session that ends with the block::

    with driven_browser(purpose="plaud-sync") as browser:
        browser.run("goto", "https://example.com")
        title = browser.run("eval", "() => document.title", "--raw")

The block reserves the browser (the Screen's lease), attaches a session under a name no other caller uses, and on the
way out, however it leaves, detaches that session and gives the lease back. The browser keeps running. A script that
attaches by hand and is killed or times out leaves the session's daemon running with nothing to end it but the
service's reaper (``screen.cli_sessions``); the block ends it at once.
"""

import os
import shutil
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from screen.cli import ServiceAnswer
from screen.cli import call_service
from screen.cli import own_chat_id
from screen.cli_sessions import PLAYWRIGHT_CLI_COMMAND
from screen.errors import BrowserSessionError
from screen.errors import BrowserUnavailableError
from screen.identity import AGENT_ID_HEADER
from screen.identity import AGENT_NAME_HEADER

DEFAULT_BROWSER_NAME: Final[str] = "browser-1"
DEFAULT_COMMAND_TIMEOUT_SECONDS: Final[float] = 300.0
_ATTACH_TIMEOUT_SECONDS: Final[float] = 120.0
_DETACH_TIMEOUT_SECONDS: Final[float] = 60.0


class DrivenBrowser(FrozenModel):
    """The attached session a ``driven_browser`` block hands its body."""

    browser_name: str = Field(description="The browser the session drives")
    session_name: str = Field(description="The session's ``-s=`` name, unique to this block")
    playwright_cli: str = Field(description="The ``playwright-cli`` executable")

    def run(self, *args: str, timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> str:
        """Run one ``playwright-cli`` command in the session and answer its output; raises
        ``BrowserSessionError`` when it fails (the CLI exits 1 for any tool error)."""
        return _run_cli(self.playwright_cli, self.session_name, list(args), timeout_seconds)


@contextmanager
def driven_browser(
    browser_name: str = DEFAULT_BROWSER_NAME, purpose: str = "script", is_reclaim: bool = False
) -> Generator[DrivenBrowser]:
    """Reserve ``browser_name``, attach a session named after ``purpose`` for the block, then detach and release.

    Raises ``BrowserUnavailableError`` (with the service's ``status``: ``busy_human``, ``busy_agent``, ``starting``,
    ``stopped`` or ``crashed``) when the browser cannot be reserved; nothing is attached then. ``is_reclaim`` takes
    the Screen back from the human, only for a run the user started themselves. A detach that fails after the body
    succeeded raises ``BrowserSessionError``; after a body that raised, it is logged and the body's error goes on.
    """
    playwright_cli = shutil.which(PLAYWRIGHT_CLI_COMMAND)
    if playwright_cli is None:
        raise BrowserSessionError(f"{PLAYWRIGHT_CLI_COMMAND} is not installed")
    headers = _agent_headers()
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
        _run_cli(playwright_cli, session_name, ["attach", f"--cdp={attach_url}"], _ATTACH_TIMEOUT_SECONDS)
        yield DrivenBrowser(browser_name=browser_name, session_name=session_name, playwright_cli=playwright_cli)
        is_body_done = True
    finally:
        detach_error = _detach(playwright_cli, session_name)
        released = call_service("POST", f"/browsers/{browser_name}/release", {}, headers)
        if released.status != 200:
            logger.warning("Releasing browser {} answered {}: {}", browser_name, released.status, released.body)
        if detach_error is not None:
            if is_body_done:
                raise detach_error
            logger.warning("{}", detach_error)


def _agent_headers() -> dict[str, str]:
    chat_id = own_chat_id()
    if not chat_id:
        raise BrowserSessionError("MNGR_AGENT_ID is not set: run the script from inside an agent")
    headers = {AGENT_ID_HEADER: chat_id, "Content-Type": "application/json"}
    name = os.environ.get("MNGR_AGENT_NAME")
    if name:
        headers[AGENT_NAME_HEADER] = name
    return headers


def _unavailable(answer: ServiceAnswer, browser_name: str, verb: str) -> BrowserUnavailableError:
    status = str(answer.body.get("status") or answer.status)
    detail = str(
        answer.body.get("hint")
        or answer.body.get("detail")
        or answer.body.get("error")
        or f"browser {browser_name} could not be reserved ({verb}: {status})"
    )
    return BrowserUnavailableError(status, detail)


def _detach(playwright_cli: str, session_name: str) -> BrowserSessionError | None:
    """Detach the session; the error to report, or None. A session the attach never started detaches cleanly."""
    try:
        _run_cli(playwright_cli, session_name, ["detach"], _DETACH_TIMEOUT_SECONDS)
    except BrowserSessionError as e:
        return BrowserSessionError(f"could not detach the playwright-cli session {session_name}: {e}")
    return None


def _run_cli(playwright_cli: str, session_name: str, args: list[str], timeout_seconds: float) -> str:
    command = [playwright_cli, f"-s={session_name}", *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as e:
        raise BrowserSessionError(f"playwright-cli {args[0]} did not finish within {timeout_seconds:.0f}s") from e
    except OSError as e:
        raise BrowserSessionError(f"playwright-cli could not run ({e})") from e
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise BrowserSessionError(f"playwright-cli {args[0]} failed: {output[-400:]}")
    return result.stdout
