"""How the chat reaches the shell over loopback: where it is, one fire-and-forget POST, and one op.

The shell is a separate app that may be down or restarting, so a fire-and-forget post that cannot be
made is a debug log, never an error: the chat keeps working without it. A shell that is up and refuses
the body is a warning: the two apps disagree about the route's shape. An op a caller acts on the
answer of (``post_layout_op``) reports the shell's answer, refusals included, and raises when the
shell cannot be reached. The auto-open reactor makes its own requests (``auto_open.py``).
"""

import time
from collections.abc import Mapping
from typing import Any
from typing import Final

import httpx
from app_manifest.shell_windows import shell_base_url as _shared_shell_base_url
from loguru import logger

from imbue.chat.errors import ChatAppError

# A post to the shell is one loopback request the shell answers without work; past the first
# threshold it is suspicious, past the second it is broken.
SHELL_POST_SLOW_SECONDS: Final[float] = 0.5
SHELL_POST_TIMEOUT_SECONDS: Final[float] = 2.0

# The shell's agent-facing op route (desktop-interface contracts.md section 8).
LAYOUT_OP_ROUTE: Final[str] = "/api/layout/broadcast"

# How much of a refusal's body the warning quotes.
_REFUSAL_DETAIL_LIMIT: Final[int] = 200


def shell_base_url() -> str:
    return _shared_shell_base_url()


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


class ShellUnreachableError(ChatAppError):
    """The shell could not be reached for an op (it is down, restarting, or did not answer in time)."""


def post_layout_op(body: Mapping[str, Any]) -> httpx.Response:
    """POST one op to the shell's op route and answer the shell's response, refusals included; raises
    ShellUnreachableError when the shell could not be reached."""
    url = f"{shell_base_url()}{LAYOUT_OP_ROUTE}"
    started_at = time.monotonic()
    try:
        response = httpx.post(url, json=body, timeout=SHELL_POST_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        raise ShellUnreachableError(f"Could not reach the shell at {url}: {e}") from e
    elapsed = time.monotonic() - started_at
    if elapsed > SHELL_POST_SLOW_SECONDS:
        logger.warning("Posted a {} op to the shell slowly, in {:.1f}s", body.get("op"), elapsed)
    return response
