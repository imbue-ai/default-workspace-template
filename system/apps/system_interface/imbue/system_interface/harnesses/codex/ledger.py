"""The codex message ledger -- the backend authority for a codex agent's message lifecycle.

The dumb frontend (contract A2) reads exactly one backend authority for a codex agent's
five message states; this is it. The ledger consumes the stock ``codex app-server``
notification stream (turn/*, item/*, thread/*) through the ONE persistent
:class:`~imbue.mngr_codex.app_server_client.CodexAppServerClient` and keeps every accepted
message in exactly one of the contract's states, keyed by the ``clientUserMessageId``
(below ``client_id``) it mints on every send:

* **Sending** -- the ``submit`` RPC opened a fresh turn and the opening ``userMessage`` has
  not committed yet (delivery is COMMIT, not ack -- contract A4).
* **Queued** -- ``submit`` parked the message as a pending steer of the running turn; it is a
  chip until it either commits or the turn settles without it.
* **Delivered** -- the ``userMessage`` item carrying our ``client_id`` committed (observed via
  ``item/completed``, or durably via the ``thread/read`` uncertainty guard). Terminal.
* **Returned** -- reconcile found our ``client_id`` NOT committed after a settle/interrupt.
  Terminal; its text goes back to the composer.

The queue is an EPHEMERAL store (contract): it lives entirely in this in-memory ledger, which
is built per live session. There is NO durable journal -- a new session builds a fresh ledger
that starts empty, and nothing from a dead session is revived or auto-sent. An idle
``thread/status`` sweeps any still-parked entry back to the composer, so the queue is empty
whenever the agent is idle.

Delivery is decided by the committed ``userMessage`` item's ``client_id``, never by the ack
(a steer can be accepted then interrupted before it commits). A ``userMessage`` whose
``clientId`` is ``null`` or not one of ours is a FOREIGN message (a human typing in the visible
``--remote`` TUI, or another client) -- shown in the transcript by the rollout watcher, but it
never creates, removes, or returns one of our chips (contract §2.6).

The ledger is a near-pure reducer over the client's notification callbacks plus the synchronous
:meth:`send`. Its one side effect is the model-bar mirror: on every ``thread/settings/updated`` it
writes the effective ``{model, effort, fast}`` to the agent's ``minds_model_state.json`` (the
event-driven replacement for the fork's write), so the shared, harness-neutral model-bar read path
reconciles the chip with no codex special-casing. All logic is therefore unit-testable by driving a
:class:`CodexAppServerClient` over a scripted in-memory transport (constructor injection), with no
live daemon.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import DispositionKind
from imbue.system_interface.activity_state import ActivityState

# The ``ThreadItem`` variant tags whose ``item/started`` (without a matching ``item/completed``)
# means a tool is running -- used to split RUNNING into TOOL_RUNNING vs THINKING for the dot.
# Pulled from the 0.147 schema's tool-bearing item types (commandExecution, mcpToolCall,
# dynamicToolCall, fileChange, collabAgentToolCall, webSearch).
_TOOL_ITEM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "commandExecution",
        "mcpToolCall",
        "dynamicToolCall",
        "fileChange",
        "collabAgentToolCall",
        "webSearch",
    }
)

# ``turn.status`` values that mean the turn ended WITHOUT committing an owned entry -> Returned.
_ABORTED_TURN_STATUSES: Final[frozenset[str]] = frozenset({"interrupted", "failed"})

# The service tier that means "fast" in the uniform model-bar schema: codex's fast toggle maps
# onto the ``priority`` service tier, so ``fast`` is exactly ``serviceTier == "priority"``.
_FAST_SERVICE_TIER: Final[str] = "priority"


class MessageState(StrEnum):
    """The contract's live states for a single accepted message (Composer is not an entry)."""

    SENDING = "sending"
    QUEUED = "queued"
    DELIVERED = "delivered"
    RETURNED = "returned"


_TERMINAL_STATES: Final[frozenset[MessageState]] = frozenset({MessageState.DELIVERED, MessageState.RETURNED})
_LIVE_STATES: Final[frozenset[MessageState]] = frozenset({MessageState.SENDING, MessageState.QUEUED})


class LedgerEntry(MutableModel):
    """One accepted message and its current live state, keyed by its minted ``client_id``.

    ``send_seq`` is a per-ledger monotonic counter assigned at Sending-entry creation; it is the
    total send order used for interrupt-return ordering, never derived from event arrival.
    ``bound_turn_id`` is the turn the ``submit`` opened (Sending) or parked into (Queued).
    ``enqueue_ts`` is the ISO timestamp the chip carries once the entry is Queued.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    client_id: str
    send_seq: int
    text: str
    state: MessageState
    bound_turn_id: str | None = None
    enqueue_ts: str = ""


def _iso_now() -> str:
    """An ISO-8601 UTC timestamp for a chip's ``enqueue_ts`` (display/order only)."""
    return datetime.now(timezone.utc).isoformat()


def _default_mint() -> str:
    """Mint a fresh ``clientUserMessageId``. A UUID so two sends never collide."""
    return f"minds-{uuid4().hex}"


class CodexMessageLedger(MutableModel):
    """The per-agent, per-session five-state message journal over the app-server event stream.

    Build with a handshaken, thread-bound :class:`CodexAppServerClient`; the ledger subscribes to
    its notifications in :meth:`build`. Drive it with :meth:`send` (mints, submits, records) and by
    letting the client dispatch notifications (which land in :meth:`handle_notification`). Read the
    queue via :meth:`queued_snapshot`, the tap gate via :meth:`is_sending`, and the dot via
    :meth:`activity_state`; :meth:`reconcile_returned` yields the Returned text in send order.
    """

    model_config = ConfigDict(frozen=False, extra="forbid", arbitrary_types_allowed=True)

    client: Any
    entries: dict[str, LedgerEntry] = Field(default_factory=dict)
    reconciled_turn_ids: set[str] = Field(default_factory=set)
    # ``client_id``s already handed back to the composer by a previous :meth:`interrupt`, so a
    # later stop never re-prepends already-returned text (each Returned message is handed off once).
    returned_handed_off: set[str] = Field(default_factory=set)
    next_send_seq: int = 0
    # Tool ``item.id``s whose ``item/started`` has no matching ``item/completed`` yet.
    open_tool_item_ids: set[str] = Field(default_factory=set)
    # The last ``thread/status`` tag observed (``idle`` / ``active`` / ...), for the idle sweep.
    status_type: str | None = None
    mint_client_id: Callable[[], str] = _default_mint
    on_queue_snapshot: Callable[[list[dict[str, str]]], None] | None = None
    on_activity: Callable[[ActivityState], None] | None = None
    # Where to mirror the effective model settings (the agent's uniform ``minds_model_state.json``).
    # ``None`` disables the mirror -- used by tests that do not exercise the model bar.
    model_state_path: Path | None = None
    now: Callable[[], str] = _iso_now
    # The last queue snapshot / activity actually pushed, so a callback fires only on a real change.
    _last_queue_snapshot: list[dict[str, str]] = []
    _last_activity: ActivityState | None = None

    @classmethod
    def build(
        cls,
        client: CodexAppServerClient,
        *,
        on_queue_snapshot: Callable[[list[dict[str, str]]], None] | None = None,
        on_activity: Callable[[ActivityState], None] | None = None,
        model_state_path: Path | None = None,
        mint_client_id: Callable[[], str] = _default_mint,
        now: Callable[[], str] = _iso_now,
    ) -> "CodexMessageLedger":
        """Build a ledger over ``client`` and subscribe it to the client's notification stream."""
        ledger = cls(
            client=client,
            mint_client_id=mint_client_id,
            on_queue_snapshot=on_queue_snapshot,
            on_activity=on_activity,
            model_state_path=model_state_path,
            now=now,
        )
        ledger._last_queue_snapshot = []
        ledger._last_activity = None
        client.add_notification_handler(ledger.handle_notification)
        return ledger

    # -- send -------------------------------------------------------------------

    def send(self, text: str, client_id: str | None = None) -> str:
        """Accept ``text`` for send: mint a ``client_id``, ``submit`` it, and record the entry.

        Idle -> the daemon opens a turn (Disposition ``STARTED``); the entry stays Sending until
        its ``userMessage`` commits (A4). Busy -> the daemon parks a steer (``STEERED``); the entry
        is Queued (a chip) at once. A transport/protocol failure returns the entry (contract Send:
        "Returned (send failed)"). Returns the minted ``client_id`` so the caller (and the browser)
        agree on the id the chip and the delivery reconcile key on.
        """
        cid = client_id if client_id is not None else self.mint_client_id()
        entry = LedgerEntry(client_id=cid, send_seq=self._next_seq(), text=text, state=MessageState.SENDING)
        self.entries[cid] = entry
        try:
            disposition = self.client.submit(text, cid)
        except CodexAppServerError as exc:
            logger.opt(exception=exc).info("codex ledger: submit failed for {}, returning to composer", cid)
            entry.state = MessageState.RETURNED
            self._emit_queue_if_changed()
            self._emit_activity_if_changed()
            return cid
        entry.bound_turn_id = disposition.turn_id
        if disposition.kind == DispositionKind.STEERED:
            entry.state = MessageState.QUEUED
            entry.enqueue_ts = self.now()
        # STARTED keeps the entry Sending: delivery waits for the committed userMessage (A4).
        self._emit_queue_if_changed()
        self._emit_activity_if_changed()
        return cid

    def _next_seq(self) -> int:
        seq = self.next_send_seq
        self.next_send_seq = seq + 1
        return seq

    # -- notification handling --------------------------------------------------

    def handle_notification(self, method: str, params: Any) -> None:
        """Reduce one app-server notification into the ledger. Registered on the client.

        Every transition is idempotent (delivered/returned are absorbing; a duplicate
        ``turn/completed`` is a no-op via ``reconciled_turn_ids``), so replays/dupes/reorders
        converge. The client updates its ``active_turn_id`` from the same frame BEFORE this runs,
        so :meth:`activity_state` reads a current view.
        """
        if not isinstance(params, dict):
            params = {}
        # A method -> reducer dispatch. turn/started is absent by design: the client tracks the
        # active turn (activity reads it), so that frame needs no ledger state and falls through to
        # the emits below. thread/settings/updated drives ONLY the model-bar mirror (no message
        # state), so it emits nothing new here.
        reducers: dict[str, Callable[[dict[str, Any]], None]] = {
            "item/started": self._on_item_started,
            "item/completed": self._on_item_completed,
            "turn/completed": self._on_turn_completed,
            "thread/status/changed": self._on_status_changed,
            "thread/settings/updated": self._on_settings_updated,
        }
        reducer = reducers.get(method)
        if reducer is not None:
            reducer(params)
        self._emit_queue_if_changed()
        self._emit_activity_if_changed()

    def _on_item_started(self, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        if item.get("type") in _TOOL_ITEM_TYPES:
            item_id = item.get("id")
            if isinstance(item_id, str):
                self.open_tool_item_ids.add(item_id)

    def _on_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type in _TOOL_ITEM_TYPES:
            item_id = item.get("id")
            if isinstance(item_id, str):
                self.open_tool_item_ids.discard(item_id)
            return
        if item_type != "userMessage":
            return
        # Delivery = COMMIT (A4): the committed userMessage carrying our client_id. A null or
        # foreign clientId is externally delivered -- never touches our chips (§2.6).
        client_id = item.get("clientId")
        if not isinstance(client_id, str):
            return
        entry = self.entries.get(client_id)
        if entry is not None and entry.state in _LIVE_STATES:
            entry.state = MessageState.DELIVERED

    def _on_turn_completed(self, params: dict[str, Any]) -> None:
        turn = params.get("turn")
        if not isinstance(turn, dict):
            return
        self._reconcile(turn)

    def _on_status_changed(self, params: dict[str, Any]) -> None:
        status = params.get("status")
        status_type = status.get("type") if isinstance(status, dict) else None
        self.status_type = status_type if isinstance(status_type, str) else None
        if self.status_type == "idle":
            self._sweep_idle()

    def _on_settings_updated(self, params: dict[str, Any]) -> None:
        """Mirror the daemon's effective model settings to the uniform ``minds_model_state.json``.

        The model bar is harness-neutral on the read side: ``agent_manager._recompute_model_choice``
        reads this file, matches it against the catalog, and pushes the chip -- one code path for
        every harness. This is the codex WRITER that feeds that path, event-driven off
        ``thread/settings/updated{threadSettings:{model, effort, serviceTier}}`` (the replacement for
        the retired fork's disk write). ``fast`` is exactly ``serviceTier == "priority"``. A write
        failure is logged, never raised: a stale chip is preferable to breaking the event stream.
        """
        if self.model_state_path is None:
            return
        settings = params.get("threadSettings")
        if not isinstance(settings, dict):
            return
        model = settings.get("model")
        if not isinstance(model, str) or not model:
            return
        effort = settings.get("effort")
        state = {
            "model": model,
            "effort": effort if isinstance(effort, str) and effort else None,
            "fast": settings.get("serviceTier") == _FAST_SERVICE_TIER,
        }
        try:
            atomic_write(self.model_state_path, json.dumps(state))
        except OSError as exc:
            logger.opt(exception=exc).warning(
                "codex ledger: failed to mirror model settings to {}", self.model_state_path
            )

    # -- reconcile / sweep ------------------------------------------------------

    def _reconcile(self, turn: dict[str, Any]) -> None:
        """Settle every owned entry bound to ``turn`` -- delivery = COMMIT, else Returned (§2.4).

        ``committed_ids`` is authoritative only from a ``turn.items`` with ``itemsView=="full"``;
        the accumulated ``item/completed`` deliveries already moved most owned entries to Delivered.
        Any still-live owned entry left unresolved after a ``completed`` turn with a non-full view
        triggers ONE ``thread/read`` uncertainty guard, which turns "did it commit?" into a
        deterministic query. Idempotent via ``reconciled_turn_ids``.
        """
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or turn_id in self.reconciled_turn_ids:
            return
        status = turn.get("status")
        items_view = turn.get("itemsView")
        committed = self._committed_ids_from_items(turn.get("items")) if items_view == "full" else set()

        owned = [entry for entry in self.entries.values() if entry.bound_turn_id == turn_id and entry.state in _LIVE_STATES]
        unresolved: list[LedgerEntry] = []
        for entry in owned:
            if entry.client_id in committed:
                entry.state = MessageState.DELIVERED
            elif status in _ABORTED_TURN_STATUSES:
                entry.state = MessageState.RETURNED
            else:
                unresolved.append(entry)

        if unresolved:
            if items_view != "full":
                committed_via_read = self._committed_ids_via_thread_read()
                for entry in unresolved:
                    entry.state = MessageState.DELIVERED if entry.client_id in committed_via_read else MessageState.RETURNED
            else:
                # A completed turn with a full item view that does not carry our id: not committed.
                for entry in unresolved:
                    entry.state = MessageState.RETURNED

        self.reconciled_turn_ids.add(turn_id)

    def _committed_ids_from_items(self, items: Any) -> set[str]:
        """The set of ``userMessage`` ``clientId``s among ``items`` (skipping null/foreign shapes)."""
        committed: set[str] = set()
        if not isinstance(items, list):
            return committed
        for item in items:
            if isinstance(item, dict) and item.get("type") == "userMessage":
                client_id = item.get("clientId")
                if isinstance(client_id, str):
                    committed.add(client_id)
        return committed

    def _committed_ids_via_thread_read(self) -> set[str]:
        """The uncertainty guard: one ``thread/read{includeTurns}`` -> committed ``clientId``s.

        A read failure yields an empty set, so an unresolved entry Returns rather than hanging --
        the conservative choice when the daemon cannot confirm the commit.
        """
        try:
            thread = self.client.thread_read(include_turns=True)
        except CodexAppServerError as exc:
            logger.opt(exception=exc).info("codex ledger: thread/read guard failed; treating unresolved as returned")
            return set()
        thread_map = thread.get("thread") if isinstance(thread, dict) else None
        turns = thread_map.get("turns") if isinstance(thread_map, dict) else None
        committed: set[str] = set()
        if isinstance(turns, list):
            for turn in turns:
                if isinstance(turn, dict):
                    committed |= self._committed_ids_from_items(turn.get("items"))
        return committed

    def _sweep_idle(self) -> None:
        """The EPHEMERAL queue backstop: at ``idle`` no turn is running, so any still-live entry is
        stranded -- its steer can never commit -- and Returns. The queue is empty whenever idle."""
        for entry in self.entries.values():
            if entry.state in _LIVE_STATES:
                entry.state = MessageState.RETURNED

    # -- shoulder-tap / interrupt (Contract B) ----------------------------------

    def is_tap_available(self) -> bool:
        """Whether a shoulder tap is offered: nothing Sending AND the queue is non-empty (Contract B).

        codex parks a busy send as a ``turn/steer`` at send time, so every Queued chip is ALREADY a
        pending steer of the running turn that auto-consumes at the next yield boundary -- a tap
        needs NO force call (it is ensure-steered). The availability gate is therefore purely: the
        tap is unavailable while anything is Sending (it must not race an in-flight send whose
        disposition is not yet known) and is a no-op when the queue is empty. Because the parked
        steers are already delivered into the running turn, an available tap has nothing to do at the
        ledger beyond confirming the gate; the messages resolve to Delivered (or stay Queued/Returned)
        through the normal turn events.
        """
        if self.is_sending():
            return False
        return any(entry.state == MessageState.QUEUED for entry in self.entries.values())

    def interrupt(self) -> str:
        """Stop the running turn and return every non-committed owned message to the composer (Contract B).

        One ``turn/interrupt`` on the tracked active turn, then ONE authoritative settle of that turn
        against the committed thread: an owned entry whose ``userMessage`` committed before the
        interrupt stays Delivered (delivery = COMMIT, not ack -- A4), and every other owned live entry
        (a parked steer, or an in-flight Sending not yet committed) Returns in ascending ``send_seq``.
        The settle routes through the :meth:`_reconcile` ``thread/read`` guard (a partial, non-aborted
        synthetic turn) so a steer that committed on the daemon but whose ``item/completed`` we have
        not yet observed is NOT wrongly returned. The dot clears immediately (A6): the interrupted turn
        is over, so ``active_turn_id`` is dropped here rather than waiting for the async
        ``turn/completed(interrupted)`` -- which :meth:`_reconcile`'s ``reconciled_turn_ids`` guard has
        already made an idempotent no-op.

        Safe and idempotent when nothing is running: with no active turn it issues no RPC and simply
        hands back whatever became non-committed since the last hand-off (e.g. an earlier idle sweep's
        Returned entries). The hand-off is once-only -- each Returned message goes to the composer
        exactly once, so a SECOND stop in the same session never re-prepends already-returned text.
        Returns that fresh Returned text as one block, in send order, for the composer (prepended).
        """
        active_turn_id = self.client.active_turn_id
        if active_turn_id is not None:
            try:
                self.client.interrupt(active_turn_id)
            except CodexAppServerError as exc:
                logger.opt(exception=exc).info(
                    "codex ledger: turn/interrupt failed; settling against the committed thread anyway"
                )
            if active_turn_id not in self.reconciled_turn_ids:
                self._reconcile({"id": active_turn_id, "status": "completed", "itemsView": "partial"})
            if self.client.active_turn_id == active_turn_id:
                self.client.active_turn_id = None
        self._emit_queue_if_changed()
        self._emit_activity_if_changed()
        return self._take_returned_block()

    # -- reads ------------------------------------------------------------------

    def queued_snapshot(self) -> list[dict[str, str]]:
        """The wire snapshot of currently-Queued chips, in send order (feeds ``update_queued_messages``)."""
        queued = sorted(
            (entry for entry in self.entries.values() if entry.state == MessageState.QUEUED),
            key=lambda entry: entry.send_seq,
        )
        return [{"queued_id": entry.client_id, "content": entry.text, "timestamp": entry.enqueue_ts} for entry in queued]

    def is_sending(self) -> bool:
        """Whether any accepted message is still Sending (the tap-availability gate)."""
        return any(entry.state == MessageState.SENDING for entry in self.entries.values())

    def reconcile_returned(self) -> str:
        """The CUMULATIVE Returned text, in send order -- every message ever Returned this session.

        A pure, non-consuming read (a snapshot for invariants/inspection). The composer hand-off is
        :meth:`_take_returned_block`, which yields each Returned message exactly once; use that, not
        this, to prepend returns, or a second stop re-emits the whole cumulative set.
        """
        returned = sorted(
            (entry for entry in self.entries.values() if entry.state == MessageState.RETURNED),
            key=lambda entry: entry.send_seq,
        )
        return "\n".join(entry.text for entry in returned)

    def _take_returned_block(self) -> str:
        """Hand back the messages Returned since the last hand-off, in send order (Contract Interrupt).

        Every Returned message reaches the composer exactly once: this yields only entries that are
        Returned and not yet handed off, then marks them, so a second :meth:`interrupt` (or a stop
        following an idle sweep that already drained) never re-prepends already-returned text.
        """
        fresh = sorted(
            (
                entry
                for entry in self.entries.values()
                if entry.state == MessageState.RETURNED and entry.client_id not in self.returned_handed_off
            ),
            key=lambda entry: entry.send_seq,
        )
        for entry in fresh:
            self.returned_handed_off.add(entry.client_id)
        return "\n".join(entry.text for entry in fresh)

    def activity_state(self) -> ActivityState:
        """The dot: TOOL_RUNNING / THINKING while a turn is in flight, IDLE otherwise (A6).

        RUNNING lasts until ``turn/completed`` (the client clears ``active_turn_id`` only on the
        terminal turn frame), NOT when token-generation stops -- so the dot stays lit through the
        turn's flush tail and clears the instant the turn is fully done.
        """
        if self.client.active_turn_id is None:
            return ActivityState.IDLE
        if self.open_tool_item_ids:
            return ActivityState.TOOL_RUNNING
        return ActivityState.THINKING

    def state_of(self, client_id: str) -> MessageState | None:
        """The current state of one accepted message, or ``None`` if it was never accepted."""
        entry = self.entries.get(client_id)
        return entry.state if entry is not None else None

    # -- change-gated callbacks -------------------------------------------------

    def _emit_queue_if_changed(self) -> None:
        if self.on_queue_snapshot is None:
            return
        snapshot = self.queued_snapshot()
        if snapshot == self._last_queue_snapshot:
            return
        self._last_queue_snapshot = snapshot
        self.on_queue_snapshot(snapshot)

    def _emit_activity_if_changed(self) -> None:
        if self.on_activity is None:
            return
        state = self.activity_state()
        if state == self._last_activity:
            return
        self._last_activity = state
        self.on_activity(state)
