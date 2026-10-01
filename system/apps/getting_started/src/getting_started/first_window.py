"""Opening the app's own window once per workspace, for the first client to connect (launcher-and-getting-started
plan section 3.4).

The desktop is generic and seeds no app's window, so the app does what the chat app does for the welcome chat
(``imbue/chat/auto_open.py``): it asks the shell, through the loopback op route, to ``open`` its page for a connected
client on the first desktop and to ``place`` that window at the left complement of the pinned chat's frame. Delivery
is remembered in a ledger under the app's state directory, so a restart opens nothing and closing the window never
brings it back. With nobody connected the open is held and retried on a fixed interval until a client appears.
"""

import threading
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from app_manifest.primitives import AppName
from getting_started.state_files import read_json_object
from getting_started.state_files import write_json_atomic
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from workspace_layout.errors import ShellOpError
from workspace_layout.interfaces import ShellLayoutInterface
from workspace_layout.ops import OpenRequest
from workspace_layout.ops import PlaceRequest
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import IfPresent

# The frame the window is placed at: the left complement of the shell's pinned frame (desktop contracts.md 4.2),
# clear of the one-column shortcut grid on a wide backdrop (launcher plan section 3.4), as ``x,y,width,height`` in
# fractions of the backdrop.
FIRST_WINDOW_FRAME: Final[str] = "0.07,0.05,0.38,0.9"
# The page the window opens at: the app's root, its one launch path.
FIRST_WINDOW_PATH: Final[str] = "/"

LEDGER_FILENAME: Final[str] = "first_window.json"
_DELIVERED_KEY: Final[str] = "is_delivered"

# The delivery thread's name, what a test looks for to tell a started opener from one left idle.
OPENER_THREAD_NAME: Final[str] = "first-window-opener"

# How often a held open is retried against the shell's client list while it is undelivered.
POLL_INTERVAL_SECONDS: Final[float] = 3.0


class FirstWindowLedger(MutableModel):
    """Whether the first-visit window has been delivered, kept in one small file so a restart opens nothing again."""

    path: Path = Field(frozen=True, description="Where the ledger is kept")

    def is_delivered(self) -> bool:
        document = read_json_object(self.path)
        return document is not None and document.get(_DELIVERED_KEY) is True

    def mark_delivered(self) -> None:
        write_json_atomic(self.path, {_DELIVERED_KEY: True})


class FirstWindowDelivery(FrozenModel):
    """What one delivery attempt came to."""

    is_delivered: bool = Field(description="Whether the window was opened and placed for a client")
    client_id: ClientId | None = Field(description="The client it was delivered to, when it was")


class FirstWindowOpener(MutableModel):
    """Delivers the app's first-visit window once, to the first connected client, from a thread of its own."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    app: AppName = Field(frozen=True, description="The app whose window is opened")
    ledger: FirstWindowLedger = Field(frozen=True, description="Whether the window has already been delivered")
    shell: ShellLayoutInterface = Field(frozen=True, description="The shell's client list, desktops, and op route")
    poll_interval_seconds: float = Field(
        default=POLL_INTERVAL_SECONDS, frozen=True, description="How often a held open is retried"
    )

    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def deliver_once(self) -> FirstWindowDelivery:
        """One attempt: nothing when the ledger says delivered or nobody is connected; else open and place the window
        for the first connected client on the first desktop, and record the delivery when both ops were accepted. A
        window the open found popped out into its own window is on screen already and is left there, unplaced."""
        if self.ledger.is_delivered():
            return FirstWindowDelivery(is_delivered=True, client_id=None)
        target = self._find_target()
        if target is None:
            return FirstWindowDelivery(is_delivered=False, client_id=None)
        client_id, desktop_id = target
        open_request = OpenRequest(
            app=self.app,
            path=FIRST_WINDOW_PATH,
            if_present=IfPresent.FOCUS,
            is_minimized=False,
            client_id=client_id,
            desktop=str(desktop_id),
        )
        try:
            opened = self.shell.open(open_request)
        except ShellOpError as e:
            logger.info("The shell did not open the {} window, so it stays owed: {}", self.app, e)
            return FirstWindowDelivery(is_delivered=False, client_id=None)
        window_id = opened.window_id
        if opened.is_raised_in_own_window:
            # The shell refuses to place a popped-out window, and every retry would raise it again.
            self.ledger.mark_delivered()
            logger.info(
                "The first-visit {} window {} is popped out for client {}; left there", self.app, window_id, client_id
            )
            return FirstWindowDelivery(is_delivered=True, client_id=client_id)
        place_request = PlaceRequest(
            window=str(window_id), frame=FIRST_WINDOW_FRAME, client_id=client_id, desktop=str(desktop_id)
        )
        try:
            self.shell.place(place_request)
        except ShellOpError as e:
            logger.info("The shell did not place window {}, so the first-visit window stays owed: {}", window_id, e)
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

    def _find_target(self) -> tuple[ClientId, DesktopId] | None:
        """The first connected client and the first desktop, or None while there is neither or the shell cannot say."""
        try:
            clients = self.shell.connected_clients()
        except ShellOpError as e:
            logger.debug("Could not list the shell's clients: {}", e)
            return None
        if not clients:
            return None
        try:
            desktops = self.shell.desktops()
        except ShellOpError as e:
            logger.debug("Could not list the shell's desktops: {}", e)
            return None
        if not desktops:
            return None
        return clients[0].id, desktops[0].id

    def start(self) -> None:
        """Start the delivery thread, unless the ledger already says delivered. Idempotent."""
        if self._thread is not None or self.ledger.is_delivered():
            return
        self._thread = threading.Thread(target=self._run, name=OPENER_THREAD_NAME, daemon=True)
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
