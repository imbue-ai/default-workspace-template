import os
import threading
import time
from collections.abc import Mapping
from typing import Any, Final

import httpx
from app_manifest.primitives import AppName
from loguru import logger
from pydantic import Field

from app_instances.interfaces import InstanceNudgerInterface

# The shell's address, resolved exactly as system/scripts/layout.py resolves it.
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"

# A post to the shell is one loopback request the shell answers without work; past the first
# threshold it is suspicious (every mutating route waits on the nudge), past the second it is
# broken.
SHELL_POST_SLOW_SECONDS: Final[float] = 0.5
SHELL_POST_TIMEOUT_SECONDS: Final[float] = 2.0


def shell_base_url() -> str:
    return os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/")


def post_to_shell(url: str, body: Mapping[str, Any] | None) -> None:
    """POST ``body`` as JSON (None for an empty body) to the shell route at ``url``.

    An unreachable or refusing shell is a debug log, never an error (until phase 7 of the model
    the shell has none of the routes apps post to), and a slow one a warning. Every post an app
    makes to the shell goes through here: the nudge, and an app's own posts to the tab routes.
    """
    started_at = time.monotonic()
    try:
        response = httpx.post(url, json=body, timeout=SHELL_POST_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        logger.debug("Skipped posting to the shell at {}: {}", url, e)
        return
    elapsed = time.monotonic() - started_at
    if elapsed > SHELL_POST_SLOW_SECONDS:
        logger.warning("Posted to the shell at {} slowly, in {:.1f}s", url, elapsed)
    if response.is_error:
        logger.debug(
            "Posted to the shell at {} and it answered {}", url, response.status_code
        )


class ShellNudger(InstanceNudgerInterface):
    """Posts ``/api/apps/<name>/changed`` to the shell; an unreachable or refusing shell is a debug log, never an error, and a slow one a warning."""

    app_name: AppName = Field(
        frozen=True, description="The registered name of the nudging app"
    )
    shell_url: str = Field(
        frozen=True, description="The shell's base URL, without a trailing slash"
    )

    def nudge(self) -> None:
        post_to_shell(f"{self.shell_url}/api/apps/{self.app_name}/changed", None)


class SilentNudger(InstanceNudgerInterface):
    """Nudges nobody: the default for an app object built before its shell nudger is installed, and for tests."""

    def nudge(self) -> None:
        return None


class ThreadedNudger(InstanceNudgerInterface):
    """Hands each nudge to a daemon thread, so a caller on a latency-sensitive thread (an event loop) never waits on the shell."""

    inner: InstanceNudgerInterface = Field(
        frozen=True, description="The nudger that actually posts to the shell"
    )

    def nudge(self) -> None:
        threading.Thread(
            target=self.inner.nudge, name="instance-nudge", daemon=True
        ).start()
