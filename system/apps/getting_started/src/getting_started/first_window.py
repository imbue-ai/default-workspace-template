"""Opening the app's own window once per workspace, for the first client to connect (launcher-and-getting-started
plan section 3.4).

The desktop is generic and seeds no app's window, so the app does what the chat app does for the welcome chat
(``imbue/chat/auto_open.py``): it asks the shell, through the loopback op route, to ``open`` its page for a connected
client on the first desktop and to ``place`` that window at the left complement of the pinned chat's frame. Delivery
is remembered in a ledger under the app's state directory, so a restart opens nothing and closing the window never
brings it back. With nobody connected the open is held and retried on a fixed interval until a client appears.
"""

import threading
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from app_manifest.primitives import AppName
from getting_started.errors import ShellAnswerError
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# The frame the window is placed at: the left complement of the shell's pinned frame (desktop contracts.md 4.2),
# clear of the one-column shortcut grid, as ``x,y,width,height`` in fractions of the backdrop.
FIRST_WINDOW_FRAME: Final[str] = "0.12,0.05,0.33,0.9"
# The page the window opens at: the app's root, its one launch path.
FIRST_WINDOW_PATH: Final[str] = "/"

LEDGER_FILENAME: Final[str] = "first_window.json"
_DELIVERED_KEY: Final[str] = "is_delivered"

# How often a held open is retried against the shell's client list while it is undelivered.
POLL_INTERVAL_SECONDS: Final[float] = 3.0
# One loopback request the shell answers without work.
SHELL_TIMEOUT_SECONDS: Final[float] = 2.0


class FirstWindowLedger(MutableModel):
    """Whether the first-visit window has been delivered, kept in one small file so a restart opens nothing again."""

    path: Path = Field(frozen=True, description="Where the ledger is kept")

    def is_delivered(self) -> bool:
        document = read_json_object(self.path)
        return document is not None and document.get(_DELIVERED_KEY) is True

    def mark_delivered(self) -> None:
        write_json_atomic(self.path, {_DELIVERED_KEY: True})


class ShellOpsInterface(MutableModel, ABC):
    """The three things the opener asks of the shell: who is connected, which desktop is first, and the two ops."""

    @abstractmethod
    def connected_client_ids(self) -> list[str]:
        """The ids of the clients holding a socket right now; empty when the shell cannot be read."""

    @abstractmethod
    def first_desktop_id(self) -> str | None:
        """The id of the first desktop (the one a workspace is born with), or None when the shell cannot be read."""

    @abstractmethod
    def open_window(self, app: AppName, path: str, client_id: str, desktop_id: str) -> str | None:
        """Open (or focus) the app's window at ``path`` for the client on the desktop; the window id, or None when refused."""

    @abstractmethod
    def place_window(self, window_id: str, frame: str, client_id: str, desktop_id: str) -> bool:
        """Place the window at ``frame`` (``x,y,width,height``) for the client; whether the shell accepted."""


@pure
def open_op_body(app: AppName, path: str, client_id: str, desktop_id: str) -> dict[str, Any]:
    """The ``open`` op (desktop contracts.md section 8): the app's window at the path, focused when one is already there."""
    return {
        "op": "open",
        "args": {"app": str(app), "path": path, "client": client_id, "desktop": desktop_id, "if_present": "focus"},
        "requester": None,
    }


@pure
def place_op_body(window_id: str, frame: str, client_id: str, desktop_id: str) -> dict[str, Any]:
    return {
        "op": "place",
        "args": {"window": window_id, "frame": frame, "client": client_id, "desktop": desktop_id},
        "requester": None,
    }


@pure
def connected_client_ids_of(payload: Any) -> list[str]:
    """The connected clients a ``GET /api/clients`` answer lists; raises ShellAnswerError for another shape."""
    clients = payload.get("clients") if isinstance(payload, dict) else None
    if not isinstance(clients, list):
        raise ShellAnswerError(f"expected a JSON object with a 'clients' list, got {type(payload).__name__}")
    return [
        str(client["id"])
        for client in clients
        if isinstance(client, dict) and client.get("is_connected") is True and client.get("id")
    ]


@pure
def first_desktop_id_of(payload: Any) -> str | None:
    """The first desktop's id in a ``GET /api/desktops`` answer, None with no desktops; raises ShellAnswerError otherwise."""
    desktops = payload.get("desktops") if isinstance(payload, dict) else None
    if not isinstance(desktops, list):
        raise ShellAnswerError(f"expected a JSON object with a 'desktops' list, got {type(payload).__name__}")
    if not desktops:
        return None
    first = desktops[0]
    if not isinstance(first, dict) or not isinstance(first.get("id"), str):
        raise ShellAnswerError("expected the first desktop to carry a string 'id'")
    return first["id"]


class HttpShellOps(ShellOpsInterface):
    """The shell over loopback: its client list, its desktops, and the agent-facing op route."""

    shell_url: str = Field(frozen=True, description="The shell's base URL, without a trailing slash")

    def _get_json(self, route: str) -> Any | None:
        try:
            response = httpx.get(f"{self.shell_url}{route}", timeout=SHELL_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Could not read {} from the shell at {}: {}", route, self.shell_url, e)
            return None

    def connected_client_ids(self) -> list[str]:
        payload = self._get_json("/api/clients")
        if payload is None:
            return []
        try:
            return connected_client_ids_of(payload)
        except ShellAnswerError as e:
            logger.warning("Ignoring the shell's client list at {}: {}", self.shell_url, e)
            return []

    def first_desktop_id(self) -> str | None:
        payload = self._get_json("/api/desktops")
        if payload is None:
            return None
        try:
            return first_desktop_id_of(payload)
        except ShellAnswerError as e:
            logger.warning("Ignoring the shell's desktops at {}: {}", self.shell_url, e)
            return None

    def _post_op(self, body: dict[str, Any], described: str) -> dict[str, Any] | None:
        """Post one op; the answer's body when the shell accepted it, None when it refused or could not be reached."""
        try:
            response = httpx.post(f"{self.shell_url}/api/layout/broadcast", json=body, timeout=SHELL_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            logger.debug("Could not ask the shell at {} to {}: {}", self.shell_url, described, e)
            return None
        if response.is_error:
            logger.info(
                "The shell refused to {} ({}): {}", described, response.status_code, response.text.strip()[:300]
            )
            return None
        try:
            payload = response.json()
        except ValueError as e:
            logger.warning("The shell answered the {} op with a body that is not JSON: {}", described, e)
            return None
        if not isinstance(payload, dict):
            logger.warning(
                "The shell answered the {} op with a JSON {} where the contract gives an object",
                described,
                type(payload).__name__,
            )
            return None
        return payload

    def open_window(self, app: AppName, path: str, client_id: str, desktop_id: str) -> str | None:
        answer = self._post_op(open_op_body(app, path, client_id, desktop_id), f"open the {app} window")
        if answer is None:
            return None
        window_id = answer.get("window_id")
        if not isinstance(window_id, str) or window_id == "":
            logger.warning("The shell accepted the open of the {} window but named no window_id: {}", app, answer)
            return None
        return window_id

    def place_window(self, window_id: str, frame: str, client_id: str, desktop_id: str) -> bool:
        return (
            self._post_op(place_op_body(window_id, frame, client_id, desktop_id), f"place window {window_id}")
            is not None
        )


class FirstWindowDelivery(FrozenModel):
    """What one delivery attempt came to."""

    is_delivered: bool = Field(description="Whether the window was opened and placed for a client")
    client_id: str | None = Field(description="The client it was delivered to, when it was")


class FirstWindowOpener(MutableModel):
    """Delivers the app's first-visit window once, to the first connected client, from a thread of its own."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    app: AppName = Field(frozen=True, description="The app whose window is opened")
    ledger: FirstWindowLedger = Field(frozen=True, description="Whether the window has already been delivered")
    shell: ShellOpsInterface = Field(frozen=True, description="The shell's client list, desktops, and op route")
    poll_interval_seconds: float = Field(
        default=POLL_INTERVAL_SECONDS, frozen=True, description="How often a held open is retried"
    )

    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def deliver_once(self) -> FirstWindowDelivery:
        """One attempt: nothing when the ledger says delivered or nobody is connected; else open and place the window
        for the first connected client on the first desktop, and record the delivery when both ops were accepted."""
        if self.ledger.is_delivered():
            return FirstWindowDelivery(is_delivered=True, client_id=None)
        client_ids = self.shell.connected_client_ids()
        if not client_ids:
            return FirstWindowDelivery(is_delivered=False, client_id=None)
        desktop_id = self.shell.first_desktop_id()
        if desktop_id is None:
            return FirstWindowDelivery(is_delivered=False, client_id=None)
        client_id = client_ids[0]
        window_id = self.shell.open_window(self.app, FIRST_WINDOW_PATH, client_id, desktop_id)
        if window_id is None:
            return FirstWindowDelivery(is_delivered=False, client_id=None)
        if not self.shell.place_window(window_id, FIRST_WINDOW_FRAME, client_id, desktop_id):
            return FirstWindowDelivery(is_delivered=False, client_id=None)
        self.ledger.mark_delivered()
        logger.info(
            "Opened the first-visit {} window {} for client {} on desktop {}",
            self.app,
            window_id,
            client_id,
            desktop_id,
        )
        return FirstWindowDelivery(is_delivered=True, client_id=client_id)

    def start(self) -> None:
        """Start the delivery thread, unless the ledger already says delivered. Idempotent."""
        if self._thread is not None or self.ledger.is_delivered():
            return
        self._thread = threading.Thread(target=self._run, name="first-window-opener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._deliver_logging_failures().is_delivered:
                return
            self._stop.wait(timeout=self.poll_interval_seconds)

    def _deliver_logging_failures(self) -> FirstWindowDelivery:
        try:
            return self.deliver_once()
        except (OSError, ValueError, RuntimeError) as e:
            # The thread outlives one bad answer from the shell; the next poll retries.
            logger.opt(exception=e).warning("A first-visit window delivery failed; retrying on the next poll")
            return FirstWindowDelivery(is_delivered=False, client_id=None)
