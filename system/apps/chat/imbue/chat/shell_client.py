"""How the chat reaches the shell over loopback: where it is, and one fire-and-forget POST.

The shell is a separate app that may be down or restarting, so a post that cannot be made is a
debug log, never an error: the chat keeps working without it. A shell that is up and refuses the
body is a warning: the two apps disagree about the route's shape. The auto-open reactor, which needs
to know whether the shell accepted an op, makes its own requests (``auto_open.py``).
"""

import os
import time
from collections.abc import Mapping
from typing import Any
from typing import Final

import httpx
from loguru import logger

# The shell's address, resolved exactly as system/scripts/layout.py resolves it.
DEFAULT_SHELL_URL: Final[str] = "http://127.0.0.1:8000"
ENV_SHELL_URL: Final[str] = "MINDS_WORKSPACE_SERVER_URL"

# A post to the shell is one loopback request the shell answers without work; past the first
# threshold it is suspicious, past the second it is broken.
SHELL_POST_SLOW_SECONDS: Final[float] = 0.5
SHELL_POST_TIMEOUT_SECONDS: Final[float] = 2.0

# How much of a refusal's body the warning quotes.
_REFUSAL_DETAIL_LIMIT: Final[int] = 200


def shell_base_url() -> str:
    return os.environ.get(ENV_SHELL_URL, DEFAULT_SHELL_URL).rstrip("/")


def post_to_shell(url: str, body: Mapping[str, Any]) -> None:
    """POST ``body`` as JSON to the shell route at ``url``; an unreachable or failing shell is a debug log, a slow
    one or one that refuses the body a warning."""
    started_at = time.monotonic()
    try:
        response = httpx.post(url, json=body, timeout=SHELL_POST_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        logger.debug("Skipped posting to the shell at {}: {}", url, e)
        return
    elapsed = time.monotonic() - started_at
    if elapsed > SHELL_POST_SLOW_SECONDS:
        logger.warning("Posted to the shell at {} slowly, in {:.1f}s", url, elapsed)
    if not response.is_error:
        return
    if response.is_client_error:
        logger.warning(
            "Posted to the shell at {} and it refused the body with {}: {}",
            url,
            response.status_code,
            response.text[:_REFUSAL_DETAIL_LIMIT],
        )
    else:
        logger.debug("Posted to the shell at {} and it answered {}", url, response.status_code)
