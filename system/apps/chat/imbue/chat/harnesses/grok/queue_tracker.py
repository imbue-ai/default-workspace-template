"""The messages the chat holds for a grok agent, and the control lane for its `/model` and `/effort` lines.

Grok's own queue lives inside its TUI where nothing can see it, so the chat never lets grok queue anything: every
message waits here, and the watcher's flush worker, the only typist, sends it once no top-level turn is in flight.
Entries are journalled under the current grok process's session token, so they survive a chat app restart and are
dropped when grok relaunches (contract Part B). A held slash command is sent alone, since grok may run it itself
without logging a user turn. The control lane is never published, journalled or returned: each command is sent
alone, `/model` first, ahead of any held message.
"""

import json
import threading
from collections.abc import Callable
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import Field

from imbue.chat.harnesses.queued_set import QueuedSet
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.tui_utils import is_slash_command_message
from imbue.mngr_grok.api import PROCESS_STARTED_MARKER_FILENAME

OUTBOX_FILENAME: Final[str] = "grok_outbox.jsonl"

_MAX_REPLAY_LINES: Final[int] = 100

# Sends of one entry that end without grok committing it before the worker stops retyping it.
MAX_DELIVERY_ATTEMPTS: Final[int] = 3

_QUEUED_ID_PREFIX: Final[str] = "grok-"

MODEL_COMMAND_VERB: Final[str] = "/model"
CONTROL_COMMAND_VERBS: Final[frozenset[str]] = frozenset({MODEL_COMMAND_VERB, "/effort"})

PublishCallback = Callable[[list[dict[str, Any]]], None]
# mngr's send through the agent's session: True once grok took the text, else SendFailedError with mngr's kind.
SessionSendCallback = Callable[[str], bool]


class GrokSendVerdict(UpperCaseStrEnum):
    """What one mngr send of held text came to."""

    # mngr typed it and confirmed grok took it.
    SENT = auto()
    # mngr typed it but could not confirm grok took it.
    UNCONFIRMED = auto()
    # grok was running a turn, so mngr typed nothing.
    BUSY = auto()
    # grok showed a screen other than its composer, so mngr typed nothing.
    BLOCKED = auto()
    # mngr typed nothing and a later send can succeed.
    RETRY = auto()
    # Nothing landed and a later send is not expected to.
    FAILED = auto()


# The verdicts on which mngr typed nothing: the claim is released and no attempt is charged.
REFUSED_VERDICTS: Final[frozenset[GrokSendVerdict]] = frozenset(
    {GrokSendVerdict.BUSY, GrokSendVerdict.BLOCKED, GrokSendVerdict.RETRY}
)
# The verdicts a POST that waited on its message fails with, taking the message back.
SEND_REJECTING_VERDICTS: Final[frozenset[GrokSendVerdict]] = frozenset(
    {GrokSendVerdict.BLOCKED, GrokSendVerdict.FAILED}
)


class GrokSendResult(FrozenModel):
    """A mngr send's verdict, with mngr's words when it failed."""

    verdict: GrokSendVerdict = Field(description="What the send came to")
    reason: str = Field(description="mngr's reason for a send that did not land, else ''")
    failure_kind: str = Field(description="mngr's SendFailureKind value for a send that did not land, else ''")


class InvalidControlCommandError(ValueError):
    """Raised for a control-lane command that is not a single `/model` or `/effort` line."""


def session_token(agent_state_dir: Path) -> str:
    """The current grok process's identity: the launch marker's mtime, or '' when grok never launched."""
    try:
        return str((agent_state_dir / PROCESS_STARTED_MARKER_FILENAME).stat().st_mtime_ns)
    except OSError:
        return ""


def _control_command_verb(command: str) -> str:
    """The command's slash verb; raises InvalidControlCommandError for anything but one `/model` or `/effort` line."""
    words = command.split()
    if "\n" in command.strip() or not words or words[0] not in CONTROL_COMMAND_VERBS:
        raise InvalidControlCommandError(f"Not a single /model or /effort line: {command!r}")
    return words[0]


@pure
def _next_block_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries one send carries, in send order: a slash command alone, else every entry before the next one."""
    if not entries:
        return []
    if is_slash_command_message(str(entries[0]["content"])):
        return entries[:1]
    block_entries: list[dict[str, Any]] = []
    for entry in entries:
        if is_slash_command_message(str(entry["content"])):
            break
        block_entries.append(entry)
    return block_entries


class GrokQueueTracker:
    """One grok agent's held messages (journal, claims, attempts, publisher) and its control lane."""

    _queued: QueuedSet
    _outbox_path: Path
    _session_token: str
    # Reentrant: a mutation publishes under it, and the publisher can re-enter through the manager.
    _lock: threading.RLock
    # Notified on every settle, so a POST waiting on its message's first send can answer.
    _settled_condition: threading.Condition
    # Ids a flush or a tap has claimed; they stay in the snapshot as Sending.
    _sending_ids: set[str]
    # Bumped when claimed work becomes void (a restart took the entries, or grok relaunched).
    _generation: int
    # How many stops are running; while any is, no flush may claim the messages a stop is about to return.
    _flush_pause_count: int
    # Accepted with no turn in the way, so the worker is about to send them; shown as Sending.
    _pending_send: set[str]
    _attempts: dict[str, int]
    # The settled first send of each entry that was Sending, until its POST reads it.
    _first_send_result_by_id: dict[str, GrokSendResult]
    # The control lane: the latest command per verb.
    _control_command_by_verb: dict[str, str]
    _session_send: SessionSendCallback | None
    _publish: PublishCallback | None
    _wake: Callable[[], None] | None

    @classmethod
    def build(cls, outbox_path: Path, session_token: str) -> "GrokQueueTracker":
        self = cls.__new__(cls)
        self._queued = QueuedSet.build()
        self._outbox_path = outbox_path
        self._session_token = session_token
        self._lock = threading.RLock()
        self._settled_condition = threading.Condition(self._lock)
        self._sending_ids = set()
        self._generation = 0
        self._flush_pause_count = 0
        self._pending_send = set()
        self._attempts = {}
        self._first_send_result_by_id = {}
        self._control_command_by_verb = {}
        self._session_send = None
        self._publish = None
        self._wake = None
        self._replay()
        return self

    # wiring

    def attach(self, *, publish: PublishCallback, wake: Callable[[], None]) -> None:
        """Bind the watcher's snapshot publisher and the flush worker's wake."""
        with self._lock:
            self._publish = publish
            self._wake = wake

    def detach(self, publish: PublishCallback) -> None:
        """Unbind the watcher that attached ``publish``; a watcher that attached since keeps its hooks."""
        with self._lock:
            if self._publish == publish:
                self._publish = None
                self._wake = None

    def bind_session_send(self, send: SessionSendCallback) -> None:
        """Hand the flush worker the agent session's send, which reports why mngr refused a send."""
        with self._lock:
            self._session_send = send

    def unbind_session_send(self, send: SessionSendCallback) -> None:
        with self._lock:
            if self._session_send == send:
                self._session_send = None

    def get_session_send(self) -> SessionSendCallback | None:
        with self._lock:
            return self._session_send

    def _wake_worker(self) -> None:
        with self._lock:
            wake = self._wake
        if wake is not None:
            wake()

    # journal

    def _replay(self) -> None:
        """Restore the entries this grok process's session journalled; a torn or foreign line is skipped."""
        if not self._session_token:
            return
        try:
            lines = self._outbox_path.read_text().splitlines()
        except OSError:
            return
        for line in lines[-_MAX_REPLAY_LINES:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                logger.opt(exception=error).warning(
                    "grok: skipping an unparsable outbox line in {}", self._outbox_path
                )
                continue
            if not isinstance(entry, dict) or entry.get("session") != self._session_token:
                continue
            queued_id = entry.get("queued_id")
            content = entry.get("content")
            timestamp = entry.get("timestamp")
            if isinstance(queued_id, str) and isinstance(content, str) and isinstance(timestamp, str):
                self._queued.add(queued_id, content, timestamp, False)

    def _write_journal_locked(self) -> None:
        entries = self._queued.snapshot()
        if not self._session_token or not entries:
            self._outbox_path.unlink(missing_ok=True)
            return
        payload = "".join(json.dumps({"session": self._session_token, **entry}) + "\n" for entry in entries)
        tmp_path = self._outbox_path.with_suffix(self._outbox_path.suffix + ".tmp")
        try:
            tmp_path.write_text(payload)
            tmp_path.replace(self._outbox_path)
        except OSError as error:
            logger.opt(exception=error).warning("grok: could not journal the queue to {}", self._outbox_path)

    def _settled_locked(self) -> None:
        """Journal and publish the queue, and wake any POST whose entry this settled."""
        self._write_journal_locked()
        self._publish_locked()
        self._settled_condition.notify_all()

    def _publish_locked(self) -> None:
        if self._publish is not None:
            self._publish(self._snapshot_locked())

    def _snapshot_locked(self) -> list[dict[str, Any]]:
        return [
            {
                **entry,
                "is_sending": entry["queued_id"] in self._sending_ids or entry["queued_id"] in self._pending_send,
            }
            for entry in self._queued.snapshot()
        ]

    def _drop_ids_locked(self, queued_ids: tuple[str, ...]) -> None:
        for queued_id in queued_ids:
            self._queued.resolve(queued_id)
            self._sending_ids.discard(queued_id)
            self._pending_send.discard(queued_id)
            self._attempts.pop(queued_id, None)

    def _clear_entries_locked(self) -> None:
        self._queued.clear()
        self._sending_ids.clear()
        self._pending_send.clear()
        self._attempts.clear()
        self._generation += 1

    # session identity

    def set_session(self, token: str) -> bool:
        """Adopt ``token``; on a change, drop every held message (never sent into a new grok). True if it changed."""
        with self._lock:
            if token == self._session_token:
                return False
            dropped_count = len(self._queued.snapshot())
            if dropped_count:
                logger.info("grok: discarding {} held message(s): grok relaunched", dropped_count)
            self._clear_entries_locked()
            self._session_token = token
            self._settled_locked()
            return True

    # held messages

    def enqueue(self, content: str, timestamp: str, *, is_turn_open: bool) -> str:
        """Hold ``content``, publish it and wake the worker; returns its queued id.

        ``is_turn_open`` False means the worker is about to send it, so it shows as Sending rather than Queued.
        """
        queued_id = f"{_QUEUED_ID_PREFIX}{uuid4().hex}"
        with self._lock:
            self._queued.add(queued_id, content, timestamp, False)
            if not is_turn_open:
                self._pending_send.add(queued_id)
            self._settled_locked()
        self._wake_worker()
        return queued_id

    def begin_flush(self) -> tuple[str, tuple[str, ...], int]:
        """Claim the next block of entries with attempts left: (block, claimed ids, generation).

        A held slash command is claimed alone, and the entries before it go first. Empty while another claim is open
        or a stop runs; that check is what keeps the worker, the tap and stop apart.
        """
        with self._lock:
            if self._sending_ids or self._flush_pause_count:
                return "", (), self._generation
            entries = _next_block_entries(
                [
                    entry
                    for entry in self._queued.snapshot()
                    if self._attempts.get(str(entry["queued_id"]), 0) < MAX_DELIVERY_ATTEMPTS
                ]
            )
            if not entries:
                return "", (), self._generation
            claimed = tuple(str(entry["queued_id"]) for entry in entries)
            self._sending_ids = set(claimed)
            self._settled_locked()
            return "\n".join(str(entry["content"]) for entry in entries), claimed, self._generation

    def pause_flushes(self) -> None:
        """Refuse every claim until the matching resume: a stop is cancelling a turn and returning what is held."""
        with self._lock:
            self._flush_pause_count += 1

    def resume_flushes(self) -> None:
        with self._lock:
            self._flush_pause_count -= 1
        self._wake_worker()

    def release_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        """Un-claim without charging an attempt: the tap claimed only to grey its button."""
        with self._lock:
            if generation != self._generation:
                return
            self._sending_ids.difference_update(claimed)
            self._settled_locked()

    def refuse_flush(self, claimed: tuple[str, ...], generation: int, result: GrokSendResult) -> None:
        """Settle a flush mngr typed nothing for: the entries wait on as Queued, charged no attempt."""
        with self._lock:
            self._record_first_send_result_locked(claimed, result)
            if generation == self._generation:
                self._sending_ids.difference_update(claimed)
                self._pending_send.difference_update(claimed)
            self._settled_locked()

    def finish_flush(
        self,
        claimed: tuple[str, ...],
        generation: int,
        *,
        delivered: tuple[str, ...],
        result: GrokSendResult,
    ) -> None:
        """Settle a flush mngr typed: ``delivered`` leave, the rest wait on as Queued with one more attempt spent.

        A stale ``generation`` (a restart or a relaunch took the entries) settles nothing.
        """
        with self._lock:
            self._record_first_send_result_locked(claimed, result)
            if generation == self._generation:
                delivered_ids = set(delivered)
                for queued_id in claimed:
                    self._sending_ids.discard(queued_id)
                    self._pending_send.discard(queued_id)
                    if queued_id in delivered_ids:
                        self._queued.resolve(queued_id)
                        self._attempts.pop(queued_id, None)
                    else:
                        self._attempts[queued_id] = self._attempts.get(queued_id, 0) + 1
            else:
                logger.info("grok: dropping a stale flush settle for {} message(s)", len(claimed))
            self._settled_locked()

    def refuse_pending(self, result: GrokSendResult) -> None:
        """Settle the unclaimed Sending entries with the refusal of the command sent ahead of them: each waits on as
        Queued, charged no attempt."""
        with self._lock:
            pending = tuple(self._pending_send - self._sending_ids)
            if not pending:
                return
            self._record_first_send_result_locked(pending, result)
            self._pending_send.difference_update(pending)
            self._settled_locked()

    def _record_first_send_result_locked(self, claimed: tuple[str, ...], result: GrokSendResult) -> None:
        for queued_id in claimed:
            if queued_id in self._pending_send:
                self._first_send_result_by_id[queued_id] = result

    def wait_for_send_result(self, queued_id: str, timeout_seconds: float) -> GrokSendResult | None:
        """Block until the first send of an entry enqueued as Sending settles; None when it was held instead, or on timeout."""
        with self._lock:
            self._settled_condition.wait_for(
                lambda: queued_id in self._first_send_result_by_id
                or (queued_id not in self._pending_send and queued_id not in self._sending_ids),
                timeout=timeout_seconds,
            )
            return self._first_send_result_by_id.pop(queued_id, None)

    def withdraw(self, queued_id: str) -> bool:
        """Take an unclaimed entry back out of the queue; False when it is claimed or gone."""
        with self._lock:
            if queued_id in self._sending_ids or not any(
                entry["queued_id"] == queued_id for entry in self._queued.snapshot()
            ):
                return False
            self._drop_ids_locked((queued_id,))
            self._settled_locked()
            return True

    def take_unclaimed(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return every unclaimed entry as one block, for stop; a claimed flush keeps its entries."""
        with self._lock:
            entries = [e for e in self._queued.snapshot() if str(e["queued_id"]) not in self._sending_ids]
            taken = tuple(str(entry["queued_id"]) for entry in entries)
            self._drop_ids_locked(taken)
            self._settled_locked()
            return "\n".join(str(entry["content"]) for entry in entries), taken

    def take_all(self) -> tuple[str, tuple[str, ...]]:
        """Remove and return every entry, claimed included, and void the claim: the restart kills its send."""
        with self._lock:
            entries = self._queued.snapshot()
            self._clear_entries_locked()
            self._settled_locked()
            return (
                "\n".join(str(entry["content"]) for entry in entries),
                tuple(str(entry["queued_id"]) for entry in entries),
            )

    def demote_pending(self) -> bool:
        """Show the unclaimed Sending entries as Queued: something is in the way. True if any changed."""
        with self._lock:
            pending = self._pending_send - self._sending_ids
            if not pending:
                return False
            self._pending_send -= pending
            self._settled_locked()
            return True

    def clear(self) -> None:
        """Drop every held message. Only for a grok session that no longer exists."""
        with self._lock:
            self._clear_entries_locked()
            self._settled_locked()

    def republish(self) -> None:
        """Announce the queue again unchanged, so a re-tracked chat gets its chips back."""
        with self._lock:
            self._publish_locked()

    # the control lane

    def enqueue_control_command(self, command: str) -> None:
        """Hold a `/model` or `/effort` line for the worker, replacing a held command of the same verb."""
        verb = _control_command_verb(command)
        with self._lock:
            self._control_command_by_verb[verb] = command.strip()
        self._wake_worker()

    def next_control_command(self) -> str | None:
        """The held `/model` first, so a held `/effort` reaches the model it was picked for."""
        with self._lock:
            model_command = self._control_command_by_verb.get(MODEL_COMMAND_VERB)
            if model_command is not None:
                return model_command
            return next(iter(self._control_command_by_verb.values()), None)

    def settle_control_command(self, command: str, result: GrokSendResult) -> None:
        """Drop a sent command, unless mngr typed nothing for it or a newer command of its verb replaced it."""
        with self._lock:
            verb = _control_command_verb(command)
            if result.verdict in REFUSED_VERDICTS or self._control_command_by_verb.get(verb) != command:
                return
            del self._control_command_by_verb[verb]
            if result.verdict is GrokSendVerdict.FAILED:
                logger.warning("grok: dropping {!r}, which mngr could not send: {}", command, result.reason)

    # reads

    def has_held_work(self) -> bool:
        """Whether anything waits for the worker: a held message or a control command."""
        with self._lock:
            return bool(self._queued.snapshot() or self._control_command_by_verb)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return self._snapshot_locked()

    def concatenated_block(self) -> str:
        with self._lock:
            return self._queued.concatenated_block()

    def has_entries(self) -> bool:
        with self._lock:
            return bool(self._queued.snapshot())

    def is_sending(self) -> bool:
        """Whether an entry is claimed or about to be sent, which withholds the shoulder tap."""
        with self._lock:
            return bool(self._sending_ids or self._pending_send)

    def is_exhausted(self, queued_id: str) -> bool:
        with self._lock:
            return self._attempts.get(queued_id, 0) >= MAX_DELIVERY_ATTEMPTS


# One tracker per agent for the agent's life, shared by its watcher, its session, the stop and tap executors and
# the model bar. Keyed by agent id alone; a grok relaunch is adopted in place by ``set_session``.
_TRACKERS: dict[str, GrokQueueTracker] = {}
_TRACKERS_LOCK: Final[threading.Lock] = threading.Lock()


def get_tracker(agent_state_dir: Path) -> GrokQueueTracker:
    """The agent's tracker, built on first use from the journal in its state dir (whose name is the agent id)."""
    agent_id = agent_state_dir.name
    with _TRACKERS_LOCK:
        existing = _TRACKERS.get(agent_id)
        if existing is not None:
            return existing
        tracker = GrokQueueTracker.build(agent_state_dir / OUTBOX_FILENAME, session_token(agent_state_dir))
        _TRACKERS[agent_id] = tracker
        return tracker


def drop_tracker(agent_id: str) -> None:
    with _TRACKERS_LOCK:
        _TRACKERS.pop(agent_id, None)


def enqueue_control_command(agent_state_dir: Path, command: str) -> None:
    """Have the agent's flush worker send a `/model` or `/effort` line alone, once no top-level turn is in flight.

    Raises InvalidControlCommandError for any other text.
    """
    get_tracker(agent_state_dir).enqueue_control_command(command)
