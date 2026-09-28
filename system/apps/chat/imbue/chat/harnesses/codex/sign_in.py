"""Signing in to ChatGPT through a short-lived `codex app-server`.

The app-server's own login RPCs replace scraping `codex login --device-auth` off a terminal: a
browser login hands back the page to open and listens for its loopback callback itself, which the
minds desktop app can relay into it; a device login hands back the page and the one-time code; and
either one reports completion as a notification rather than an exit status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final
from typing import Protocol

from imbue.mngr_codex.app_server_client import ChatgptDeviceLoginStart
from imbue.mngr_codex.app_server_client import ChatgptLoginStart
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import LoginCompleted
from imbue.mngr_codex.app_server_client import connect_app_server_transport

# Marks an app-server this app started for a sign-in, so a later boot can tell it from the
# app-servers the account's own chats run on and reap only the orphaned sign-ins.
SIGN_IN_FLOW_ENV_VAR: Final = "MINDS_SIGN_IN_FLOW"
APP_SERVER_SOCKET_FILENAME: Final = "codex.sock"
_CLIENT_NAME: Final = "minds-chat-sign-in"
_CLIENT_VERSION: Final = "1"


class CodexLoginClient(Protocol):
    """The login slice of the app-server client, so tests can script a sign-in."""

    def start_chatgpt_login(self) -> ChatgptLoginStart: ...

    def start_device_login(self) -> ChatgptDeviceLoginStart: ...

    def wait_login_completed(self, login_id: str, timeout_seconds: float) -> LoginCompleted: ...

    def close(self) -> None: ...


def app_server_argv(socket_path: Path) -> list[str]:
    """The arguments that start an app-server listening on `socket_path`."""
    return ["app-server", "--listen", f"unix://{socket_path}"]


def connect_login_client(socket_path: Path) -> CodexLoginClient:
    """Connect to the app-server on `socket_path` and complete its handshake."""
    client = CodexAppServerClient(transport=connect_app_server_transport(socket_path))
    is_ready = False
    try:
        client.initialize(_CLIENT_NAME, _CLIENT_VERSION)
        is_ready = True
    finally:
        if not is_ready:
            client.close()
    return client
