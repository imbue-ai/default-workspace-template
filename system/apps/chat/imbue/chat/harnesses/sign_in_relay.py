"""The workspace half of a relayed provider sign-in.

A provider's browser sign-in ends on a loopback redirect (`http://localhost:<port>/callback?...`)
to a listener the sign-in CLI opened inside this workspace. The user's browser runs on their own
machine, where nothing listens on that port, so the minds desktop app listens there instead and
hands the callback to the chat app, which replays it against the CLI here.

This module is that replay: it reads the loopback callback a sign-in URL names, and delivers the
callback to the CLI's listener. What the listener answers is not passed back: claude sends the
browser to its success page before its token exchange has succeeded, so the flow's own verdict is
what the desktop app is told.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from imbue.chat.harnesses.pty_auth import PtyAuthError
from imbue.imbue_common.frozen_model import FrozenModel

# What a sign-in CLI is given as `$BROWSER`, relative to the workspace root.
BROWSER_SHIM_RELATIVE_PATH: Final = Path("system/scripts/minds_browser_shim")
BROWSER_ENV_VAR: Final = "BROWSER"
SIGN_IN_URL_FILE_ENV_VAR: Final = "MINDS_SIGNIN_URL_FILE"
SIGN_IN_URL_FILENAME: Final = "relay_url"

# How long the CLI may take to answer the relayed callback: it answers once its token exchange ends.
CALLBACK_TIMEOUT_SECONDS: Final = 30.0
MAX_PATH_AND_QUERY_LENGTH: Final = 8192

_LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
_CALLBACK_PATHS: Final = frozenset({"/callback", "/auth/callback"})
_MIN_CALLBACK_PORT: Final = 1024
_MAX_CALLBACK_PORT: Final = 65535


class RelayTarget(FrozenModel):
    """The loopback callback a sign-in URL redirects to, and the `state` its first request must carry."""

    port: int = Field(description="The port the sign-in CLI listens on inside this workspace")
    path: str = Field(description="The callback path the provider redirects to")
    state: str = Field(description="The OAuth state the provider echoes back on the callback")


class RelayCallbackError(PtyAuthError):
    """A relayed request could not be delivered to the sign-in CLI."""


def _single(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name, [])
    return values[0] if len(values) == 1 and values[0] else None


def parse_relay_target(sign_in_url: str) -> RelayTarget | None:
    """The loopback callback `sign_in_url` names, or None when it names none this relay will serve.

    The desktop app validates the same URL again before it listens or opens anything; this side
    only has to know which local port and `state` the flow's callback belongs to.
    """
    parts = urlsplit(sign_in_url)
    if parts.scheme != "https":
        return None
    query = parse_qs(parts.query)
    redirect_uri = _single(query, "redirect_uri")
    state = _single(query, "state")
    if redirect_uri is None or state is None:
        return None
    redirect = urlsplit(redirect_uri)
    try:
        port = redirect.port
    except ValueError:
        return None
    if (
        redirect.scheme != "http"
        or redirect.hostname not in _LOOPBACK_HOSTS
        or port is None
        or not _MIN_CALLBACK_PORT <= port <= _MAX_CALLBACK_PORT
        or redirect.path not in _CALLBACK_PATHS
    ):
        return None
    return RelayTarget(port=port, path=redirect.path, state=state)


def query_state(path_and_query: str) -> str | None:
    """The `state` a relayed request carries, or None."""
    return _single(parse_qs(urlsplit(path_and_query).query), "state")


def is_relayable_path(path_and_query: str) -> bool:
    """Whether `path_and_query` is a local path this relay may replay: never a scheme or another host."""
    return (
        0 < len(path_and_query) <= MAX_PATH_AND_QUERY_LENGTH
        and path_and_query.startswith("/")
        and not path_and_query.startswith("//")
        and "\\" not in path_and_query
    )


def read_sign_in_url(url_file: Path) -> str | None:
    """The URL the browser shim recorded, once it has finished writing it."""
    try:
        written = url_file.read_text()
    except FileNotFoundError:
        return None
    if not written.endswith("\n"):
        return None
    url = written.strip()
    return url or None


def _same_listener_redirect(response: httpx.Response, port: int) -> str | None:
    """The path a redirect sends the browser to on the same loopback listener, or None for any other answer."""
    if not response.is_redirect:
        return None
    target = urlsplit(str(response.url.join(response.headers.get("location", ""))))
    try:
        target_port = target.port
    except ValueError:
        return None
    if target.scheme != "http" or target.hostname not in _LOOPBACK_HOSTS or target_port != port:
        return None
    path_and_query = f"{target.path}?{target.query}" if target.query else target.path
    return path_and_query if is_relayable_path(path_and_query) else None


def fetch_loopback_callback(port: int, path_and_query: str) -> None:
    """Deliver the callback to the sign-in CLI's listener, waiting for it to answer.

    A redirect back to the same listener is followed, once: codex answers its callback by sending
    the browser to its own `/success` page, and only finishes the login when that page is asked for.
    A redirect anywhere else is not, since claude's goes off to the provider's own page.

    Raises RelayCallbackError when the listener cannot be reached or does not answer in time.
    """
    deadline = time.monotonic() + CALLBACK_TIMEOUT_SECONDS
    try:
        response = httpx.get(
            f"http://127.0.0.1:{port}{path_and_query}", timeout=CALLBACK_TIMEOUT_SECONDS, follow_redirects=False
        )
        redirect_path = _same_listener_redirect(response, port)
        if redirect_path is not None:
            httpx.get(
                f"http://127.0.0.1:{port}{redirect_path}",
                timeout=max(0.1, deadline - time.monotonic()),
                follow_redirects=False,
            )
    except httpx.HTTPError as e:
        raise RelayCallbackError(f"the sign-in did not answer ({type(e).__name__})") from e


CallbackFetcher = Callable[[int, str], None]
