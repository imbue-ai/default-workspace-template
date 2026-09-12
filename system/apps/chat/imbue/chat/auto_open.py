"""Surfacing the tab of a chat created from outside the workspace, once, where the user is.

A chat the Minds app starts -- the update run behind "Update now", the help chat behind "Ask
an agent" -- carries a label asking for its tab to be opened when it appears. The app cannot
dock a tab itself: it is outside the workspace, and the user may not be looking yet. So the
chat app reacts to the label on a newly observed agent and asks the shell to open the chat's
address in every connected client, which files it into whatever view each client is on.

A chat is owed its tab exactly once. Delivery is remembered on disk, so a restart of this app
(the update run itself restarts it) neither re-pops a tab the user has since closed nor loses
one nobody was there to take: with no client connected the open is held and retried until a
client connects, for as long as the chat exists. Only a chat the ledger already names is left
to the saved layout -- along with the chats a workspace already had the first time this app
kept a ledger at all, which are adopted as shown rather than each popping a tab.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from typing import Protocol
from typing import runtime_checkable

import httpx
from app_instances.nudge import SHELL_POST_TIMEOUT_SECONDS
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel

logger = _loguru_logger

# ``assist`` is the label the Minds app's help flow has always set; ``auto_open`` is the
# purpose-neutral form any spawner can set. The app sets both.
AUTO_OPEN_LABELS: Final[tuple[str, ...]] = ("auto_open", "assist")

# How often a held open is retried against the shell's client list while anything is pending.
# The shell has no hook for a client arriving, so a window opened later is found by asking.
FLUSH_INTERVAL_SECONDS: Final[float] = 3.0

# Beside the chat app's other per-workspace state (see ``message_stamps``).
DEFAULT_LEDGER_PATH: Final[Path] = Path("data/.apps/chat/auto_opened_chats.json")

_DELIVERED_KEY: Final = "delivered"


def is_auto_open_labeled(labels: Mapping[str, str]) -> bool:
    return any(labels.get(label) == "true" for label in AUTO_OPEN_LABELS)


def chat_address(agent_id: str) -> str:
    return f"app:chat?instance={agent_id}"


class AutoOpenLedger(MutableModel):
    """The set of chat agent ids whose open has reached a client.

    A ``path`` of None keeps the set in memory only (tests, and a boot with no workspace to
    persist into).
    """

    model_config = {"extra": "forbid", "frozen": False}

    path: Path | None = Field(description="Where the set is kept, or None for memory only")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _delivered: set[str] = PrivateAttr(default_factory=set)
    _is_history_known: bool = PrivateAttr(default=True)

    def model_post_init(self, context: object, /) -> None:
        self._delivered, self._is_history_known = self._load()

    def _load(self) -> tuple[set[str], bool]:
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
        return {str(agent_id) for agent_id in delivered}, True

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

    def is_delivered(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._delivered

    def mark_delivered(self, agent_id: str) -> None:
        with self._lock:
            if agent_id in self._delivered:
                return
            self._delivered.add(agent_id)
            self._save_unlocked()

    def adopt_delivered(self, agent_ids: Iterable[str]) -> None:
        """Take chats as already shown without showing them, and leave a ledger behind either way.

        What a first boot with no ledger finds is history this app cannot see: every chat the
        Minds app ever labeled here, back to the workspace's first day. The file is written
        even when there is nothing to adopt, so that its existence is what tells the next boot
        the set it reads is the real one.
        """
        with self._lock:
            self._delivered.update(agent_ids)
            self._is_history_known = True
            self._save_unlocked()

    def forget(self, agent_id: str) -> None:
        """Drop a destroyed agent's entry, so the ledger only ever names live chats."""
        with self._lock:
            if agent_id not in self._delivered:
                return
            self._delivered.discard(agent_id)
            self._save_unlocked()


@runtime_checkable
class ShellLayoutInterface(Protocol):
    """The two things the reactor asks of the shell: who is connected, and to open a chat for one of them."""

    def connected_client_ids(self) -> list[str]: ...

    def open_chat(self, agent_id: str, client_id: str) -> bool: ...


class ShellLayoutClient(FrozenModel):
    """The shell over loopback: its client list, and its agent-facing op route (contracts.md section 12).

    Unlike ``post_to_shell`` this reports whether the shell accepted the op, because the
    reactor holds an open the shell refused and tries again.
    """

    shell_url: str = Field(description="The shell's base URL, without a trailing slash")

    def connected_client_ids(self) -> list[str]:
        # An answer of the wrong shape reads as no clients, rather than subscripting blind: an
        # exception here escapes the flush thread's own catch and ends it for the life of the
        # process, and a reactor with no thread surfaces no tab and says nothing about it.
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

    def open_chat(self, agent_id: str, client_id: str) -> bool:
        body = {"op": "open", "args": {"address": chat_address(agent_id), "client": client_id}, "requester": ""}
        try:
            response = httpx.post(
                f"{self.shell_url}/api/layout/broadcast", json=body, timeout=SHELL_POST_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as e:
            logger.debug("Could not ask the shell at {} to open chat {}: {}", self.shell_url, agent_id, e)
            return False
        if response.is_error:
            logger.info(
                "The shell refused to open chat {} for client {} ({}): {}",
                agent_id,
                client_id,
                response.status_code,
                response.text.strip()[:300],
            )
            return False
        return True


class DisconnectedShell(FrozenModel):
    """A shell with nobody connected: the default until ``main`` installs the real one, and for tests."""

    def connected_client_ids(self) -> list[str]:
        return []

    def open_chat(self, agent_id: str, client_id: str) -> bool:
        return False


class AutoOpenReactor(MutableModel):
    """Delivers the open a labeled chat is owed, once, to the clients connected when it can.

    ``flush`` is what delivers: it is run from a thread of its own, woken by every change to
    the pending set and otherwise every ``FLUSH_INTERVAL_SECONDS`` while anything is pending,
    so a client arriving later is found without the shell having to say so.
    """

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    ledger: AutoOpenLedger = Field(description="Which chats' opens have already reached a client")
    shell: ShellLayoutInterface = Field(description="The shell's client list and op route")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pending_agent_ids: set[str] = PrivateAttr(default_factory=set)
    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def note_appeared(self, agent_id: str, labels: Mapping[str, str]) -> None:
        """A labeled agent the observe stream just added is owed its open unless it already had it."""
        if not is_auto_open_labeled(labels) or self.ledger.is_delivered(agent_id):
            return
        with self._lock:
            if agent_id in self._pending_agent_ids:
                return
            self._pending_agent_ids.add(agent_id)
        self._wake.set()

    def seed_at_startup(self, agents: Mapping[str, Mapping[str, str]]) -> None:
        """Decide what each labeled chat found at startup is owed: its open, or nothing.

        A restart normally restores the saved layout rather than reopening tabs, so a chat the
        ledger already names stays as the user left it. One it does not name is still owed its
        open -- an update run started while no client was connected, whose apply then restarted
        this app before anyone looked -- and holds it for as long as the chat exists.

        Except on a boot with no ledger to read, where a chat the ledger does not name means
        nothing: every labeled chat the workspace has is adopted as shown, since the ledger is
        the only thing that could tell the one chat owed a tab from a year of delivered ones.
        """
        if not self.ledger.is_history_known:
            adopted = [agent_id for agent_id, labels in agents.items() if is_auto_open_labeled(labels)]
            self.ledger.adopt_delivered(adopted)
            logger.info(
                "Adopted {} labeled chat(s) as already shown: this workspace had no auto-open ledger to read",
                len(adopted),
            )
            return
        for agent_id, labels in agents.items():
            self.note_appeared(agent_id, labels)

    def forget(self, agent_id: str) -> None:
        with self._lock:
            self._pending_agent_ids.discard(agent_id)
        self.ledger.forget(agent_id)

    def pending_agent_ids(self) -> set[str]:
        with self._lock:
            return set(self._pending_agent_ids)

    def flush(self) -> None:
        """Try every held open against every connected client; the first accepted open delivers it."""
        pending = self.pending_agent_ids()
        if not pending:
            return
        client_ids = self.shell.connected_client_ids()
        if not client_ids:
            return
        for agent_id in pending:
            accepted = [client_id for client_id in client_ids if self.shell.open_chat(agent_id, client_id)]
            if accepted:
                logger.info("Opened chat {} in {} client(s): {}", agent_id, len(accepted), ", ".join(accepted))
                self.ledger.mark_delivered(agent_id)
                self._drop(agent_id)

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
            if self.pending_agent_ids():
                self._flush_logging_failures()

    def _flush_logging_failures(self) -> None:
        try:
            self.flush()
        except (OSError, ValueError, RuntimeError) as e:
            # The thread has to outlive one bad answer from the shell; the next wake retries.
            logger.opt(exception=e).warning("An auto-open flush failed; retrying on the next wake")

    def _drop(self, agent_id: str) -> None:
        with self._lock:
            self._pending_agent_ids.discard(agent_id)
