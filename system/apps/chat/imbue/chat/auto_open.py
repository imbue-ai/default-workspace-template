"""Surfacing a chat created from outside the workspace, once, where the user is.

A chat the Mind app starts -- the welcome chat it seeds, the update run behind "Update now", the
help chat behind "Ask an agent" -- carries a label asking to be shown when it appears. The app
cannot show it itself: it is outside the workspace, and the user may not be looking yet. So the
chat app reacts to the label on a newly observed agent and asks the shell's ``show`` op
(desktop-interface contracts.md section 8) to put the chat root with the chat selected on the
screen of every connected client, on whatever desktop each client is on. It names no page to
repoint, so the shell raises a window already showing the chat, else points this app's pinned
window at it (pinned-taskbar-entries plan section 4.8; the pinned window is independent, so each
client's own view moves and nobody else's does), else opens a chat root window on it.

A chat is owed its window exactly once. Delivery is remembered on disk, so a restart of this app
(the update run itself restarts it) neither re-pops a window the user has since closed nor loses
one nobody was there to take: with no client connected the show is held and retried until a
client connects, for as long as the chat exists. Only a chat the ledger already names is left
to the saved layout -- along with the chats a workspace already had the first time this app
kept a ledger at all, which are adopted as shown rather than each popping a window.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.primitives import ChatId
from imbue.chat.shell_client import ShellLayoutInterface
from imbue.chat.shell_client import ShellOpError
from imbue.chat.shell_client import ShowRequest
from imbue.imbue_common.mutable_model import MutableModel

logger = _loguru_logger

# ``assist`` is the label the Mind app's help flow has always set; ``auto_open`` is the
# purpose-neutral form any spawner can set. The app sets both.
AUTO_OPEN_LABELS: Final[tuple[str, ...]] = ("auto_open", "assist")

# How often a held open is retried against the shell's client list while anything is pending.
# The shell has no hook for a client arriving, so a window opened later is found by asking.
FLUSH_INTERVAL_SECONDS: Final[float] = 3.0

# Beside the chat app's other per-workspace state (see ``message_stamps``), in its data directory.
LEDGER_FILENAME: Final[str] = "auto_opened_chats.json"

_DELIVERED_KEY: Final = "delivered"

# The chat root: the chat list, beside whichever chat is selected.
CHAT_ROOT_PAGE: Final[str] = "/"


def is_auto_open_labeled(labels: Mapping[str, str]) -> bool:
    return any(labels.get(label) == "true" for label in AUTO_OPEN_LABELS)


def chat_root_path(chat_id: ChatId) -> str:
    """The chat root's path with the chat selected (plan section 9.1): where the auto-opened window lands."""
    return f"{CHAT_ROOT_PAGE}?chat={chat_id}"


class AutoOpenLedger(MutableModel):
    """The set of chat ids whose open has reached a client.

    A ``path`` of None keeps the set in memory only (tests, and a boot with no workspace to
    persist into).
    """

    model_config = {"extra": "forbid", "frozen": False}

    path: Path | None = Field(description="Where the set is kept, or None for memory only")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _delivered: set[ChatId] = PrivateAttr(default_factory=set)
    _is_history_known: bool = PrivateAttr(default=True)

    def model_post_init(self, context: object, /) -> None:
        self._delivered, self._is_history_known = self._load()

    def _load(self) -> tuple[set[ChatId], bool]:
        if self.path is None:
            # Nothing is kept here at all, so an empty set is the whole history rather than a lost one.
            return set(), True
        if not self.path.exists():
            return set(), False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.opt(exception=e).warning("Ignoring an unreadable auto-open ledger at {}", self.path)
            return set(), False
        delivered = data.get(_DELIVERED_KEY) if isinstance(data, dict) else None
        if not isinstance(delivered, list):
            logger.warning(
                "Ignoring an auto-open ledger of the wrong shape at {} (expected a JSON object with a "
                "'{}' list, got {})",
                self.path,
                _DELIVERED_KEY,
                type(data).__name__,
            )
            return set(), False
        return {ChatId(str(chat_id)) for chat_id in delivered}, True

    def _save_unlocked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps({_DELIVERED_KEY: sorted(self._delivered)}), encoding="utf-8")
            os.replace(tmp_path, self.path)
        except OSError as e:
            logger.opt(exception=e).warning("Failed to write the auto-open ledger at {}", self.path)

    @property
    def is_history_known(self) -> bool:
        """Whether the delivered set is the whole history of what this workspace has been shown.

        False when there was a file to read and it did not read: a workspace whose chats
        predate this app keeping a ledger at all, or one whose ledger has been lost since.
        """
        return self._is_history_known

    def is_delivered(self, chat_id: ChatId) -> bool:
        with self._lock:
            return chat_id in self._delivered

    def mark_delivered(self, chat_id: ChatId) -> None:
        with self._lock:
            if chat_id in self._delivered:
                return
            self._delivered.add(chat_id)
            self._save_unlocked()

    def adopt_delivered(self, chat_ids: Iterable[ChatId]) -> None:
        """Take chats as already shown without showing them, and leave a ledger behind either way.

        What a first boot with no ledger finds is history this app cannot see: every chat the
        Mind app ever labeled here, back to the workspace's first day. The file is written
        even when there is nothing to adopt, so that its existence is what tells the next boot
        the set it reads is the real one.
        """
        with self._lock:
            self._delivered.update(chat_ids)
            self._is_history_known = True
            self._save_unlocked()

    def forget(self, chat_id: ChatId) -> None:
        """Drop a destroyed chat's entry, so the ledger only ever names live chats."""
        with self._lock:
            if chat_id not in self._delivered:
                return
            self._delivered.discard(chat_id)
            self._save_unlocked()


class AutoOpenReactor(MutableModel):
    """Delivers the open a labeled chat is owed, once, to the clients connected when it can.

    ``flush`` is what delivers: it is run from a thread of its own, woken by every change to
    the pending set and otherwise every ``FLUSH_INTERVAL_SECONDS`` while anything is pending,
    so a client arriving later is found without the shell having to say so.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    ledger: AutoOpenLedger = Field(description="Which chats' opens have already reached a client")
    shell: ShellLayoutInterface = Field(description="The shell's client list and its show op")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pending_chat_ids: set[ChatId] = PrivateAttr(default_factory=set)
    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def note_appeared(self, chat_id: ChatId, labels: Mapping[str, str]) -> None:
        """A chat whose labeled agent the observe stream just added is owed its open unless it already had it.

        A successor agent of an existing chat never carries the label, so a handoff never re-pops a window.
        """
        if not is_auto_open_labeled(labels):
            return
        self.request_open(chat_id)

    def request_open(self, chat_id: ChatId) -> None:
        """A chat this app opened on its own (a seeded chat) is owed its window like a labeled one.

        Held and retried the same way, so a chat seeded while nobody was connected (the Mind
        app seeds the workspace's first chat before its window shows the workspace) gets its
        window when the first client connects.
        """
        if self.ledger.is_delivered(chat_id):
            return
        with self._lock:
            if chat_id in self._pending_chat_ids:
                return
            self._pending_chat_ids.add(chat_id)
        self._wake.set()

    def seed_at_startup(self, labels_by_chat_id: Mapping[ChatId, Mapping[str, str]]) -> None:
        """Decide what each labeled chat found at startup is owed: its open, or nothing.

        A restart normally restores the saved layout rather than reopening windows, so a chat the
        ledger already names stays as the user left it. One it does not name is still owed its
        open -- an update run started while no client was connected, whose apply then restarted
        this app before anyone looked -- and holds it for as long as the chat exists.

        Except on a boot with no ledger to read, where a chat the ledger does not name means
        nothing: every labeled chat the workspace has is adopted as shown, since the ledger is
        the only thing that could tell the one chat owed a window from a year of delivered ones.
        """
        if not self.ledger.is_history_known:
            adopted = [chat_id for chat_id, labels in labels_by_chat_id.items() if is_auto_open_labeled(labels)]
            self.ledger.adopt_delivered(adopted)
            logger.info(
                "Adopted {} labeled chat(s) as already shown: this workspace had no auto-open ledger to read",
                len(adopted),
            )
            return
        for chat_id, labels in labels_by_chat_id.items():
            self.note_appeared(chat_id, labels)

    def forget(self, chat_id: ChatId) -> None:
        with self._lock:
            self._pending_chat_ids.discard(chat_id)
        self.ledger.forget(chat_id)

    def pending_chat_ids(self) -> set[ChatId]:
        with self._lock:
            return set(self._pending_chat_ids)

    def flush(self) -> None:
        """Try every held chat against every connected client; the first client the shell shows it to delivers it."""
        pending = self.pending_chat_ids()
        if not pending:
            return
        client_ids = self.shell.connected_client_ids()
        if not client_ids:
            return
        for chat_id in pending:
            accepted = [client_id for client_id in client_ids if self._is_shown(chat_id, client_id)]
            if accepted:
                logger.info("Opened chat {} in {} client(s): {}", chat_id, len(accepted), ", ".join(accepted))
                self.ledger.mark_delivered(chat_id)
                self._drop(chat_id)

    def start(self) -> None:
        """Start the flush thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="auto-open-flush", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=FLUSH_INTERVAL_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                return
            if self.pending_chat_ids():
                self._flush_logging_failures()

    def _flush_logging_failures(self) -> None:
        try:
            self.flush()
        except (OSError, ValueError, RuntimeError) as e:
            # The thread has to outlive one bad answer from the shell; the next wake retries.
            logger.opt(exception=e).warning("An auto-open flush failed; retrying on the next wake")

    def _is_shown(self, chat_id: ChatId, client_id: str) -> bool:
        """Ask the shell to show the chat root on the chat to one client; whichever way it shows it counts."""
        request = ShowRequest(path=chat_root_path(chat_id), showing=(), repoint=(), client_id=client_id)
        try:
            self.shell.show(request)
        except ShellOpError as e:
            logger.info("The shell did not show chat {} to client {}, so it is held: {}", chat_id, client_id, e)
            return False
        return True

    def _drop(self, chat_id: ChatId) -> None:
        with self._lock:
            self._pending_chat_ids.discard(chat_id)
