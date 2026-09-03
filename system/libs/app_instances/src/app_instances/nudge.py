import os
from typing import Final

import httpx
from app_manifest.primitives import AppName
from loguru import logger
from pydantic import Field

from app_instances.interfaces import InstanceNudgerInterface

# The shell's address, resolved exactly as system/scripts/layout.py resolves it.
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"

NUDGE_TIMEOUT_SECONDS: Final[float] = 2.0


def shell_base_url() -> str:
    return os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/")


class ShellNudger(InstanceNudgerInterface):
    """Posts ``/api/apps/<name>/changed`` to the shell; an unreachable or refusing shell is a debug log, never an error."""

    app_name: AppName = Field(
        frozen=True, description="The registered name of the nudging app"
    )
    shell_url: str = Field(
        frozen=True, description="The shell's base URL, without a trailing slash"
    )

    def nudge(self) -> None:
        url = f"{self.shell_url}/api/apps/{self.app_name}/changed"
        try:
            response = httpx.post(url, timeout=NUDGE_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            logger.debug("Skipped nudging the shell at {}: {}", url, e)
            return
        if response.is_error:
            logger.debug(
                "Nudged the shell at {} and it answered {}", url, response.status_code
            )
