"""How the chat reaches the shell over loopback: where it is, one fire-and-forget POST, and the layout client.

The shell is a separate app that may be down or restarting, so a fire-and-forget post that cannot be
made is a debug log, never an error: the chat keeps working without it. A shell that is up and refuses
the body is a warning: the two apps disagree about the route's shape. The layout client
(``ShellLayoutClient``) is for what this app acts on the answer of: the shell's connected clients, and
its ``show`` op, which raises a ``ShellOpError`` when the shell does not show what it was asked to.
"""

import time
from collections.abc import Mapping
from typing import Any
from typing import Final
from typing import Protocol
from typing import runtime_checkable

import httpx
from app_manifest.shell_windows import shell_base_url as _shared_shell_base_url
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.chat.errors import ChatAppError
from imbue.chat.primitives import CHAT_APP_NAME
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# A post to the shell is one loopback request the shell answers without work; past the first
# threshold it is suspicious, past the second it is broken.
SHELL_POST_SLOW_SECONDS: Final[float] = 0.5
SHELL_POST_TIMEOUT_SECONDS: Final[float] = 2.0

# The shell's agent-facing op route (desktop-interface contracts.md section 8).
LAYOUT_OP_ROUTE: Final[str] = "/api/layout/broadcast"

# How much of a refusal's body a warning or an error quotes.
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


class ShellOpError(ChatAppError):
    """The shell did not carry out an op this app asked for."""


class ShellUnreachableError(ShellOpError):
    """The shell could not be reached for an op (it is down, restarting, or did not answer in time)."""


class ShellRefusedOpError(ShellOpError):
    """The shell answered an op with an error status."""


class ShellAnswerMalformedError(ShellOpError):
    """The shell answered an op with a success status and a body that is not the op's answer."""


class ShowRequest(FrozenModel):
    """A ``show`` of one of this app's paths on one client's screen (desktop-interface contracts.md section 8)."""

    path: str = Field(description="The path to put on the client's screen")
    showing: tuple[str, ...] = Field(description="The app's other paths that count as already showing it")
    repoint: tuple[str, ...] = Field(
        description="The pages (paths without a query string) whose windows the shell may point at the path"
    )
    client_id: str = Field(description="The client whose screen it goes on")


class ShowAnswer(FrozenModel):
    """What the shell answered a ``show``: how it put the path on screen, and the window it used."""

    # The rest of the shell's answer (the desktop, the layout) is the shell's to add to.
    model_config = ConfigDict(frozen=True, extra="ignore")

    shown: str = Field(description="How the shell showed the path, as it spells it (raised, navigated, ...)")
    window_id: str = Field(description="The window now showing the path")


@pure
def show_op_body(request: ShowRequest) -> dict[str, Any]:
    """The op route's body for ``request``, under this app's requester."""
    return {
        "op": "show",
        "args": {
            "app": CHAT_APP_NAME,
            "path": request.path,
            "showing": list(request.showing),
            "repoint": list(request.repoint),
            "client": request.client_id,
        },
        "requester": {"app": CHAT_APP_NAME, "marker": ""},
    }


@runtime_checkable
class ShellLayoutInterface(Protocol):
    """What this app asks of the shell's layout: who is connected, and to show one of its paths to one of them."""

    def connected_client_ids(self) -> list[str]: ...

    def show(self, request: ShowRequest) -> ShowAnswer: ...


class ShellLayoutClient(FrozenModel):
    """The shell over loopback: its client list, and its agent-facing op route (desktop-interface contracts.md section 8).

    Unlike ``post_to_shell`` this reports whether the shell did what it was asked, because its callers act on it:
    the auto-open reactor holds a chat the shell did not show and tries again, and the focus-chat route answers
    with what the shell did.
    """

    shell_url: str = Field(description="The shell's base URL, without a trailing slash")

    def connected_client_ids(self) -> list[str]:
        # An answer of the wrong shape reads as no clients, rather than subscripting blind: an
        # exception here escapes the auto-open flush thread's own catch and ends it for the life of
        # the process, and a reactor with no thread surfaces no window and says nothing about it.
        try:
            response = httpx.get(f"{self.shell_url}/api/clients", timeout=SHELL_POST_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Could not list the shell's clients at {}: {}", self.shell_url, e)
            return []
        clients = payload.get("clients") if isinstance(payload, dict) else None
        if not isinstance(clients, list):
            logger.warning(
                "Ignoring a client list of the wrong shape from the shell at {} (expected a JSON object with a "
                "'clients' list, got {})",
                self.shell_url,
                type(payload).__name__,
            )
            return []
        return [
            str(client["id"])
            for client in clients
            if isinstance(client, dict) and client.get("is_connected") and client.get("id")
        ]

    def show(self, request: ShowRequest) -> ShowAnswer:
        """Ask the shell's ``show`` op to put the path on the client's screen and answer what it did. Raises
        ShellUnreachableError, ShellRefusedOpError, or ShellAnswerMalformedError when it did not."""
        url = f"{self.shell_url}{LAYOUT_OP_ROUTE}"
        started_at = time.monotonic()
        try:
            response = httpx.post(url, json=show_op_body(request), timeout=SHELL_POST_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            raise ShellUnreachableError(f"Could not reach the shell at {url}: {e}") from e
        elapsed = time.monotonic() - started_at
        if elapsed > SHELL_POST_SLOW_SECONDS:
            logger.warning("Asked the shell for a show slowly, in {:.1f}s", elapsed)
        detail = response.text.strip()[:_REFUSAL_DETAIL_LIMIT]
        if response.is_error:
            raise ShellRefusedOpError(f"The shell refused the show ({response.status_code}): {detail}")
        try:
            return ShowAnswer.model_validate_json(response.content)
        except ValidationError as e:
            raise ShellAnswerMalformedError(f"The shell answered the show with something else: {detail}") from e


class DisconnectedShell(FrozenModel):
    """A shell with nobody connected and nothing to show on: the stand-in where no shell is wired (a test, a
    secondary chat)."""

    def connected_client_ids(self) -> list[str]:
        return []

    def show(self, request: ShowRequest) -> ShowAnswer:
        raise ShellUnreachableError(f"No shell is connected to show {request.path} to client {request.client_id}")
