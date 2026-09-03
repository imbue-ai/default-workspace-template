import os
import time
from typing import Final

import httpx
from app_manifest.primitives import AppName
from loguru import logger
from pydantic import Field

from app_instances.interfaces import InstanceNudgerInterface

# The shell's address, resolved exactly as system/scripts/layout.py resolves it.
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"

# A nudge is one loopback POST the shell answers without work; past the first threshold it
# is suspicious (every mutating route waits on it), past the second it is broken.
NUDGE_SLOW_SECONDS: Final[float] = 0.5
NUDGE_TIMEOUT_SECONDS: Final[float] = 2.0


def shell_base_url() -> str:
    return os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/")


class ShellNudger(InstanceNudgerInterface):
    """Posts ``/api/apps/<name>/changed`` to the shell; an unreachable or refusing shell is a debug log, never an error, and a slow one a warning."""

    app_name: AppName = Field(
        frozen=True, description="The registered name of the nudging app"
    )
    shell_url: str = Field(
        frozen=True, description="The shell's base URL, without a trailing slash"
    )

    def nudge(self) -> None:
        url = f"{self.shell_url}/api/apps/{self.app_name}/changed"
        started_at = time.monotonic()
        try:
            response = httpx.post(url, timeout=NUDGE_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            logger.debug("Skipped nudging the shell at {}: {}", url, e)
            return
        elapsed = time.monotonic() - started_at
        if elapsed > NUDGE_SLOW_SECONDS:
            logger.warning("Nudged the shell at {} slowly, in {:.1f}s", url, elapsed)
        if response.is_error:
            logger.debug(
                "Nudged the shell at {} and it answered {}", url, response.status_code
            )
