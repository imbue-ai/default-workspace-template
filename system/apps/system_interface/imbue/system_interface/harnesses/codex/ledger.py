"""The codex message ledger -- the backend authority for a codex agent's message lifecycle.

The dumb frontend (contract A2) reads exactly one backend authority for a codex agent's
five message states; this is it. The ledger consumes the stock ``codex app-server``
notification stream (turn/*, item/*, thread/*) through the ONE persistent
:class:`~imbue.mngr_codex.app_server_client.CodexAppServerClient` and keeps every accepted
message in exactly one of the contract's states.

Identity (contract A4, Fix 2). The delivery/return decision keys on the app-server's own
``item.id`` -- the id codex assigns each committed ``userMessage`` -- which the ledger records
on the entry when the message commits. The minted ``client_id`` (``clientUserMessageId``) is NOT
the delivery key; it is only the correlation TOKEN that links our optimistic Sending/Queued entry
to the committed item, because codex assigns no ``item.id`` until commit (a parked steer has no
``item.id`` at all until the yield boundary consumes it -- verified live), so at send time the
token is the only handle we have. When the committed item arrives it echoes the token back, we
adopt its ``item.id``, and delivery is decided by that committed item (never by the ack).

Source-agnostic (contract A3, Fix 1/2). The subscribed ledger owns the LIVE user-turn: on any
``userMessage`` commit -- ours, a second client's, or one typed into the ``--remote`` TUI -- it
emits the user-turn to the transcript (via ``on_user_turn``), first removing the chip if the
message was one of our parked steers (the A3b ordered handoff: chip out, then turn in). The
rollout file reader suppresses live user-turns and re-emits them only on hydration; the two agree
on the user-turn ``event_id`` (keyed on the echoed ``client_id``, else the commit epoch-ms +
content) so the same message never double-shows. A FOREIGN commit (no token of ours) is emitted
as a live user-turn but is NOT one of our tracked entries: it never creates, removes, or returns a
chip, and does not count toward our five-state conservation (it was never accepted through our UI).

Our own accepted messages are each in exactly one of the contract's states:

* **Sending** -- the ``submit`` RPC opened a fresh turn and the opening ``userMessage`` has
  not committed yet (delivery is COMMIT, not ack -- contract A4).
* **Queued** -- ``submit`` parked the message as a pending steer of the running turn; it is a
  chip until it either commits or the turn settles without it.
* **Delivered** -- the ``userMessage`` item carrying our correlation token committed (observed
  via ``item/completed``, or durably via the ``thread/read`` uncertainty guard); the entry adopts
  the committed item's ``item.id``. Terminal.
* **Returned** -- reconcile found our token NOT committed after a settle/interrupt. Terminal; its
  text goes back to the composer.

The queue is an EPHEMERAL store (contract): it lives entirely in this in-memory ledger, which
is built per live session. There is NO durable journal -- a new session builds a fresh ledger
that starts empty, and nothing from a dead session is revived or auto-sent. An idle
``thread/status`` sweeps any still-parked entry back to the composer, so the queue is empty
whenever the agent is idle.

Delivery is decided by the committed ``userMessage`` item, never by the ack (a steer can be
accepted then interrupted before it commits). A ``userMessage`` whose ``clientId`` is ``null`` or
not one of ours is a FOREIGN message (a human typing in the visible ``--remote`` TUI, or another
client) -- the ledger emits its live user-turn (source-agnostic), but it never creates, removes,
or returns one of our chips and is not a tracked entry.

The ledger is a near-pure reducer over the client's notification callbacks plus the synchronous
:meth:`send`. Its one side effect is the model-bar mirror: on every ``thread/settings/updated`` it
writes the effective ``{model, effort, fast}`` to the agent's ``model_state.json`` (the
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
from imbue.system_interface.harnesses.codex.session_parser import build_user_turn_event
from imbue.system_interface.harnesses.codex.session_parser import codex_user_turn_event_id

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

# The separator between the parked messages a shoulder-tap concatenates into one combined
# ``turn/start`` (Fix 3): a blank line, so the re-sent block reads as distinct paragraphs. The
# committed userMessage echoes this exact text back, so the emitted user-turn matches the rollout.
_COMBINED_RESEND_SEPARATOR: Final[str] = "\n\n"


class MessageState(StrEnum):
    """The contract's live states for a single accepted message (Composer is not an entry)."""

    SENDING = "sending"
    QUEUED = "queued"
    DELIVERED = "delivered"
    RETURNED = "returned"


_TERMINAL_STATES: Final[frozenset[MessageState]] = frozenset({MessageState.DELIVERED, MessageState.RETURNED})
_LIVE_STATES: Final[frozenset[MessageState]] = frozenset({MessageState.SENDING, MessageState.QUEUED})


class LedgerEntry(MutableModel):
    """One of OUR accepted messages and its current live state, keyed by its correlation token.

    ``client_id`` is the minted ``clientUserMessageId`` correlation token -- the entry's dict key
    and what codex echoes back on the committed item so we can link the commit to this entry. It is
    NOT the delivery decision (contract A4): delivery is the committed item, and ``item_id`` records
    that committed item's app-server ``item.id`` once observed (``None`` while still in flight, since
    codex assigns no id until commit). ``send_seq`` is a per-ledger monotonic counter assigned at
    Sending-entry creation; it is the total send order used for interrupt-return ordering, never
    derived from event arrival. ``bound_turn_id`` is the turn the ``submit`` opened (Sending) or
    parked into (Queued). ``enqueue_ts`` is the ISO timestamp the chip carries once the entry is
    Queued.

    ``combined_client_id`` is set on a queued entry that a shoulder-tap re-sent as part of a single
    combined ``turn/start`` (Fix 3): its own ``client_id`` no longer names the message the daemon
    commits (the combined turn carries ONE fresh id), so delivery is decided by that combined id.
    ``resend_visible`` is True while such an entry is mid-resend (Sending): it stays a visible chip
    rendered "Sending..." through the interrupt+resend so it never blinks out (contract A1a).
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    client_id: str
    send_seq: int
    text: str
    state: MessageState
    item_id: str | None = None
    bound_turn_id: str | None = None
    enqueue_ts: str = ""
    combined_client_id: str | None = None
    resend_visible: bool = False


class ShoulderTapResult(MutableModel):
    """The outcome of a :meth:`CodexMessageLedger.shoulder_tap` the endpoint maps to its response.

    ``status`` is ``"send_in_flight"`` (a raw send is in flight -- a benign no-op the availability
    flag already greys), ``"no_open_turn"`` (nothing was queued), or ``"tapped"`` (the queue was
    delivered early via the combined resend). ``returned_block`` is non-empty ONLY when the combined
    resend itself failed to submit: the parked text is then handed back to the composer through the
    endpoint's response (in send order) so it is never swallowed (contract A1a) -- the same
    drain-to-composer hand-off Stop uses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    returned_block: str = ""


def write_codex_model_state(model_state_path: Path, model: str, effort: str | None, fast: bool) -> None:
    """Write the uniform ``{model, effort, fast}`` model-bar state file for codex.

    The shared, harness-neutral writer for codex's live model chip: the ledger calls it on every
    ``thread/settings/updated`` (the selected settings), and the live connection calls it once on
    connect to seed from the ``thread/resume`` settings (§8). Both feed the same harness-neutral read
    path (``agent_manager._recompute_model_choice``). A write failure is logged, never raised: a stale
    chip is preferable to breaking the caller (the event stream, or a connect)."""
    state = {"model": model, "effort": effort if isinstance(effort, str) and effort else None, "fast": fast}
    try:
        atomic_write(model_state_path, json.dumps(state))
    except OSError as exc:
        logger.opt(exception=exc).warning("codex: failed to write model state to {}", model_state_path)


def _iso_now() -> str:
    """An ISO-8601 UTC timestamp for a chip's ``enqueue_ts`` (display/order only)."""
    return datetime.now(timezone.utc).isoformat()


def _default_mint() -> str:
    """Mint a fresh ``clientUserMessageId``. A UUID so two sends never collide."""
    return f"minds-{uuid4().hex}"


def _user_message_item_text(content: Any) -> str:
    """Join the text of an app-server ``userMessage`` item's ``content`` blocks.

    The item carries ``content: [{"type": "text", "text": ...}, ...]`` (verified live); a foreign
    turn has no ledger entry to read the text from, so this is the source of its user-turn body."""
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def _as_int(value: Any) -> int | None:
    """Return ``value`` as an int when it is an int-like number, else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _epoch_ms_to_iso(epoch_ms: int) -> str:
    """An ISO-8601 UTC timestamp for a user-turn event, from a commit's epoch-ms."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


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
    # ``event_id``s of user-turns already emitted through ``on_user_turn`` this session, so a
    # message observed twice (e.g. ``item/completed`` then the ``turn/completed`` reconcile, or a
    # duplicate frame) emits its live user-turn exactly once. The frontend also dedups by
    # ``event_id``, so this is belt-and-suspenders, but it keeps the broadcast clean.
    emitted_user_turn_keys: set[str] = Field(default_factory=set)
    # ``client_id``s already handed back to the composer by a previous :meth:`interrupt`, so a
    # later stop never re-prepends already-returned text (each Returned message is handed off once).
    returned_handed_off: set[str] = Field(default_factory=set)
    next_send_seq: int = 0
    # Bumped on every :meth:`interrupt`. A :meth:`send` captures it before its ``submit`` RPC and
    # re-reads it after; a change means an interrupt cleared the turn WHILE this send was mid-submit,
    # so the send raced onto a now-idle daemon -- it reconciles itself to Returned rather than leave a
    # stray Sending/Queued entry (or silently open a fresh turn on the interrupted daemon). Fix 4.
    interrupt_generation: int = 0
    # Tool ``item.id``s whose ``item/started`` has no matching ``item/completed`` yet.
    open_tool_item_ids: set[str] = Field(default_factory=set)
    # The last ``thread/status`` tag observed (``idle`` / ``active`` / ...), for the idle sweep.
    status_type: str | None = None
    mint_client_id: Callable[[], str] = _default_mint
    on_queue_snapshot: Callable[[list[dict[str, Any]]], None] | None = None
    # Emits ONE committed user-turn event (the ``build_user_turn_event`` shape) to the transcript
    # stream. The ledger owns live user-turns (Fix 1); the rollout file reader suppresses them live.
    # ``None`` disables emission -- used by tests that only assert queue/state transitions.
    on_user_turn: Callable[[dict[str, Any]], None] | None = None
    # Where to mirror the effective model settings (the agent's uniform ``model_state.json``).
    # ``None`` disables the mirror -- used by tests that do not exercise the model bar.
    model_state_path: Path | None = None
    now: Callable[[], str] = _iso_now
    # The last queue snapshot actually pushed, so the callback fires only on a real change.
    _last_queue_snapshot: list[dict[str, Any]] = []
    # User-turn events built during the current notification, flushed AFTER the queue snapshot so a
    # committed steer's chip-removal is broadcast before its transcript turn (A3b ordered handoff).
    _pending_user_turns: list[dict[str, Any]] = []

    @classmethod
    def build(
        cls,
        client: CodexAppServerClient,
        *,
        on_queue_snapshot: Callable[[list[dict[str, Any]]], None] | None = None,
        on_user_turn: Callable[[dict[str, Any]], None] | None = None,
        model_state_path: Path | None = None,
        mint_client_id: Callable[[], str] = _default_mint,
        now: Callable[[], str] = _iso_now,
    ) -> "CodexMessageLedger":
        """Build a ledger over ``client`` and subscribe it to the client's notification stream."""
        ledger = cls(
            client=client,
            mint_client_id=mint_client_id,
            on_queue_snapshot=on_queue_snapshot,
            on_user_turn=on_user_turn,
            model_state_path=model_state_path,
            now=now,
        )
        ledger._last_queue_snapshot = []
        ledger._pending_user_turns = []
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
        generation = self.interrupt_generation
        try:
            disposition = self.client.submit(text, cid)
        except CodexAppServerError as exc:
            logger.opt(exception=exc).info("codex ledger: submit failed for {}, returning to composer", cid)
            entry.state = MessageState.RETURNED
            self._emit_queue_if_changed()
            return cid
        entry.bound_turn_id = disposition.turn_id
        if disposition.kind == DispositionKind.STEERED:
            entry.state = MessageState.QUEUED
            entry.enqueue_ts = self.now()
        # STARTED keeps the entry Sending: delivery waits for the committed userMessage (A4).
        # Late-submit guard (Fix 4): if an interrupt fired while this ``submit`` was in flight, the
        # send raced onto a just-cleared turn -- reconcile it straight to Returned rather than leave a
        # stray live entry that the interrupt already handed its block back without.
        if self.interrupt_generation != generation and entry.state in _LIVE_STATES:
            entry.state = MessageState.RETURNED
        self._emit_queue_if_changed()
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
        # A3b ordered handoff: the queue snapshot (chip removal) is broadcast FIRST, then the
        # committed user-turn, so a message leaving the queue is never shown as a chip and a turn
        # at once. Activity last (it does not participate in the handoff).
        self._emit_queue_if_changed()
        self._flush_user_turns()

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
        # Delivery = COMMIT (A4): a committed userMessage. Its ``clientId`` is the correlation
        # token; if it names one of our live entries we deliver that entry and adopt the committed
        # ``item.id``. Otherwise the commit is FOREIGN (another client / the --remote TUI): we do
        # not track it as an entry, but we DO emit its live user-turn source-agnostically (A3).
        client_id = item.get("clientId")
        item_id = item.get("id")
        content = _user_message_item_text(item.get("content"))
        commit_ms = _as_int(params.get("completedAtMs"))
        item_id_str = item_id if isinstance(item_id, str) else None
        entry = self.entries.get(client_id) if isinstance(client_id, str) else None
        if entry is not None and entry.state != MessageState.DELIVERED:
            # A single send's own committed userMessage: deliver it, using the entry's own text (the
            # exact bytes the user sent). Chip-removal (if a parked steer) is emitted before the turn.
            # A committed userMessage is DEFINITIVE proof of commit (delivery = COMMIT, A4), so it also
            # corrects an entry we optimistically Returned during an async interrupt (the
            # accepted-then-committed micro-race): prefer the committed truth over the optimistic
            # Returned. A genuinely-failed/aborted entry never receives an ``item/completed`` for its
            # ``client_id``, so only that micro-race can reach a non-live entry here.
            self._deliver([entry], entry.client_id, commit_ms, entry.text, item_id_str)
            return
        # A combined shoulder-tap resend commits ONE userMessage carrying a fresh ``combined_client_id``
        # (Fix 3): deliver EVERY member of that group at once, emitting a SINGLE user-turn (the
        # concatenated content the daemon committed), keyed on the combined id so the rollout copy dedups.
        group = self._resend_group(client_id) if isinstance(client_id, str) else []
        if group:
            self._deliver(group, client_id, commit_ms, content or self._combined_text(group), item_id_str)
            return
        if entry is None:
            # Foreign commit -- emit its live user-turn (keyed on its own clientId when tagged, else
            # anon), but create no entry: it is not one of our five-state messages.
            self._queue_user_turn(client_id if isinstance(client_id, str) else None, commit_ms, content)

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
        """Mirror the daemon's effective model settings to the uniform ``model_state.json``.

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
        write_codex_model_state(
            self.model_state_path,
            model,
            effort if isinstance(effort, str) and effort else None,
            settings.get("serviceTier") == _FAST_SERVICE_TIER,
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

        owned = [
            entry for entry in self.entries.values() if entry.bound_turn_id == turn_id and entry.state in _LIVE_STATES
        ]
        unresolved = self._settle_committed(owned, committed)
        if unresolved:
            if status in _ABORTED_TURN_STATUSES:
                # A terminal (interrupted/failed) turn commits nothing further: the rest Returns.
                still_uncommitted = unresolved
            elif items_view != "full":
                # A non-authoritative view: ONE thread/read decides "did it commit?" deterministically.
                still_uncommitted = self._settle_committed(unresolved, self._committed_ids_via_thread_read())
            else:
                # A completed turn with a full item view that does not carry our id: not committed.
                still_uncommitted = unresolved
            for entry in still_uncommitted:
                entry.state = MessageState.RETURNED

        self.reconciled_turn_ids.add(turn_id)

    def _settle_committed(self, entries: list[LedgerEntry], committed: set[str]) -> list[LedgerEntry]:
        """Deliver every entry in ``entries`` whose committed key is in ``committed``; return the rest.

        The committed key is the entry's own ``client_id`` (a single send) OR its
        ``combined_client_id`` (a shoulder-tap resend, whose members all share ONE committed id).
        Entries sharing a committed key are delivered as ONE group (one emitted user-turn), so a
        combined resend never emits per-member duplicates. No ``item.id`` is available on this
        settle path (the entries' own ``item/completed`` was not observed), so it is left as-is.
        """
        groups: dict[str, list[LedgerEntry]] = {}
        uncommitted: list[LedgerEntry] = []
        for entry in entries:
            key = self._committed_key(entry, committed)
            if key is None:
                uncommitted.append(entry)
            else:
                groups.setdefault(key, []).append(entry)
        for key, group in groups.items():
            if len(group) == 1 and group[0].combined_client_id is None:
                self._deliver(group, group[0].client_id, None, group[0].text, None)
            else:
                self._deliver(group, key, None, self._combined_text(group), None)
        return uncommitted

    @staticmethod
    def _committed_key(entry: LedgerEntry, committed: set[str]) -> str | None:
        """The committed clientId that delivers ``entry`` (its own, else its combined id), or None."""
        if entry.client_id in committed:
            return entry.client_id
        if entry.combined_client_id is not None and entry.combined_client_id in committed:
            return entry.combined_client_id
        return None

    def _deliver(
        self,
        entries: list[LedgerEntry],
        event_client_id: str | None,
        epoch_ms: int | None,
        content: str,
        item_id: str | None,
    ) -> None:
        """Mark ``entries`` Delivered and emit ONE committed user-turn for them (deduped by event_id).

        A single send delivers one entry with its own text; a combined shoulder-tap resend delivers
        the whole group with the concatenated content, both keyed on the committed userMessage's
        ``client_id`` so the rollout file reader's copy dedups against this live one (A3b)."""
        for entry in entries:
            if item_id is not None:
                entry.item_id = item_id
            entry.state = MessageState.DELIVERED
            entry.resend_visible = False
        self._queue_user_turn(event_client_id, epoch_ms, content)

    def _resend_group(self, client_id: str) -> list[LedgerEntry]:
        """The live entries a shoulder-tap re-sent under the combined ``client_id`` (Fix 3)."""
        return [
            entry
            for entry in self.entries.values()
            if entry.state in _LIVE_STATES and entry.combined_client_id == client_id
        ]

    @staticmethod
    def _combined_text(entries: list[LedgerEntry]) -> str:
        """The concatenated text of a shoulder-tap resend group, in send order (Fix 3)."""
        return _COMBINED_RESEND_SEPARATOR.join(
            entry.text for entry in sorted(entries, key=lambda entry: entry.send_seq)
        )

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
        settled and the queue ends empty. Reconciles via ONE ``thread/read`` rather than a blind
        return (Fix 4, defense in depth): a steer that committed on the daemon just before the idle
        edge stays Delivered; only genuinely uncommitted entries Return. With the connection
        subscribed, ``turn/completed`` has usually reconciled already, so this typically finds nothing
        live and issues no read.

        A shoulder-tap's in-flight resend entries are EXCLUDED: the tap deliberately drives the daemon
        idle (it interrupts before re-sending), so the idle this sweep sees is the tap's own -- the
        resend is about to open a fresh turn on the (background) reader's watch, and returning those
        messages here would blink them out mid-resend (A1a). The tap owns their fate (deliver on the
        combined commit, or Return if its own resend fails)."""
        self._settle_live_to_returned(include_resend=False)

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
        """Stop the running turn and return every non-committed live message to the composer (Contract B).

        ASYNC (contract A5, Fix 4). The composer block is computed from the ledger's CURRENT live
        state and returned IMMEDIATELY; the dot clears IMMEDIATELY; the multi-second ``turn/interrupt``
        RPC is fired FIRE-AND-FORGET and never blocks the response. Concretely:

        * Every still-live entry -- a parked steer (Queued), an in-flight Sending still mid-``submit``
          (no turn binding yet), and a shoulder-tap's resend chips alike -- Returns now, in ascending
          ``send_seq``. So the required interrupt-during-flush case (queue non-empty AND a message
          in-flight Sending) returns BOTH together, in send order. A message that already committed is
          Delivered (delivery = COMMIT, not ack -- A4) via the subscribed ``item/completed`` and is no
          longer live, so it is not returned.
        * The interrupted turn is over, so ``active_turn_id`` is dropped here (the dot goes IDLE at
          once, A6) and the turn is pre-marked reconciled so the out-of-band
          ``turn/completed(interrupted)`` is an idempotent no-op.
        * ``turn/interrupt`` is sent fire-and-forget -- we do NOT wait for the abort (up to several
          seconds mid-stream). The authoritative per-id reconcile completes when
          ``turn/completed(interrupted)`` lands on the subscribed stream. The accepted-then-committed
          micro-race (a live entry that committed JUST before the interrupt, whose ``item/completed``
          had not yet arrived) is optimistically Returned here and then CORRECTED to Delivered by its
          late ``item/completed`` -- :meth:`_on_item_completed` prefers the committed truth, so it is
          never double-counted.

        Bumps the interrupt generation so a send whose ``submit`` was mid-flight reconciles itself to
        Returned rather than opening a stray turn on the now-idle daemon (the in-flight-send race guard).

        Safe and idempotent when nothing is running: with no active turn and nothing live it issues no
        RPC and simply hands back whatever became non-committed since the last hand-off. The hand-off is
        once-only -- each Returned message goes to the composer exactly once, so a SECOND stop in the
        same session never re-prepends already-returned text. Returns that fresh Returned text as one
        block, in send order, for the composer (prepended).
        """
        self.interrupt_generation += 1
        active_turn_id = self.client.active_turn_id
        # Optimistic settle from CURRENT live state (no blocking thread/read): everything still live
        # Returns; a committed message is already Delivered (A4) and not live, so it stays.
        for entry in self.entries.values():
            if entry.state in _LIVE_STATES:
                entry.state = MessageState.RETURNED
                entry.resend_visible = False
        if active_turn_id is not None:
            self.reconciled_turn_ids.add(active_turn_id)
            if self.client.active_turn_id == active_turn_id:
                self.client.active_turn_id = None
            # Fire-and-forget: do not block the response on the multi-second abort (A5).
            self.client.interrupt_nowait(active_turn_id)
        # Same A3b ordering as a notification: chip-removal snapshot first, then any user-turn, then
        # activity (the dot, now IDLE).
        self._emit_queue_if_changed()
        self._flush_user_turns()
        return self._take_returned_block()

    def _settle_live_to_returned(self, *, include_resend: bool = True) -> None:
        """Settle live entries against the committed thread: committed -> Delivered, rest Returned.

        The idle backstop's reconcile (Fix 4, defense in depth), run on the background reader thread --
        NOT on the user's request path, so its ``thread/read`` never blocks Stop/the tap (those settle
        optimistically without a read). One ``thread/read`` (only when something is live) yields the
        committed clientIds; a message committed just before the idle edge stays Delivered, and every
        still-uncommitted live entry Returns. The automatic idle backstop passes ``include_resend=False``
        so it never steals a tap's in-flight resend; ``include_resend=True`` would settle those too."""
        live = [
            entry
            for entry in self.entries.values()
            if entry.state in _LIVE_STATES and (include_resend or not entry.resend_visible)
        ]
        if not live:
            return
        still_uncommitted = self._settle_committed(live, self._committed_ids_via_thread_read())
        for entry in still_uncommitted:
            entry.state = MessageState.RETURNED

    def shoulder_tap(self) -> ShoulderTapResult:
        """Deliver the parked queue EARLY by interrupting the running turn and re-sending it (Fix 3).

        The tap is a DISTINCT ledger path from :meth:`interrupt`: the stop *returns* the queue to the
        composer, the tap *delivers* it. Codex exposes no early-flush, so end-and-resend IS the
        deliver-now mechanism -- frame-for-frame the ``--remote`` TUI's own "send early" (verified
        live). ASYNC (contract A5): the multi-second ``turn/interrupt`` is fired FIRE-AND-FORGET so the
        endpoint never blocks on the abort; only the fast ``turn/start`` resend is awaited (so a resend
        failure can be handed back in the response, below). The steps, keeping the queue continuously
        visible (A1a):

        1. Capture the Queued messages in send order and flip each to a resend-visible **Sending**
           state, so it stays a chip rendered "Sending..." through the whole interrupt+resend -- never
           removed to the composer, never blinked out. (Gated benign no-op if not available.)
        2. ``turn/interrupt`` the running turn fire-and-forget and clear it locally.
        3. Reconcile per committed id: a steer that already committed (observed via ``item/completed``)
           is Delivered -- not Queued -- so it is not captured and not re-sent; only genuinely-uncommitted
           chips ride the resend. (An unobserved just-before-interrupt commit is the accepted micro-race:
           it is re-sent and then corrected to Delivered by its own late ``item/completed``.)
        4. ``turn/start`` ONE fresh turn carrying the remaining messages concatenated in send order
           (combining required -- individually the first would open a turn and the rest re-queue). All
           remaining chips share that combined turn's fresh ``client_id``; when it commits they resolve
           to Delivered together (chip removal, then the one committed turn -- A3b). If that
           ``turn/start`` itself FAILS, the parked text is not lost: every member Returns and the
           Returned block is handed back to the composer via the result (A1a), never swallowed.

        Returns a :class:`ShoulderTapResult`: ``"send_in_flight"`` (a raw send is in flight -- benign
        no-op, the availability flag already greys the button), ``"no_open_turn"`` (nothing queued), or
        ``"tapped"`` (queue delivered early / re-sent; ``returned_block`` non-empty only if the resend
        submit failed and the text was handed back).
        """
        if self.is_sending():
            return ShoulderTapResult(status="send_in_flight")
        queued = sorted(
            (entry for entry in self.entries.values() if entry.state == MessageState.QUEUED),
            key=lambda entry: entry.send_seq,
        )
        if not queued:
            return ShoulderTapResult(status="no_open_turn")

        # (1) Flip the parked steers to resend-visible Sending -- they stay on screen the whole time.
        self.interrupt_generation += 1
        for entry in queued:
            entry.state = MessageState.SENDING
            entry.resend_visible = True
        self._emit_queue_if_changed()

        # (2) End the running turn fire-and-forget: do not block on the multi-second abort (A5).
        active_turn_id = self.client.active_turn_id
        if active_turn_id is not None:
            self.reconciled_turn_ids.add(active_turn_id)
            if self.client.active_turn_id == active_turn_id:
                self.client.active_turn_id = None
            self.client.interrupt_nowait(active_turn_id)

        # (3) The messages to re-send are the resend chips that have not (already) committed. A steer
        # that committed before the tap is Delivered via the subscribed item/completed and no longer a
        # resend chip, so it is naturally excluded -- reconcile-per-id without a blocking thread/read.
        remaining = sorted(
            (entry for entry in queued if entry.state == MessageState.SENDING and entry.resend_visible),
            key=lambda entry: entry.send_seq,
        )
        if not remaining:
            self._emit_queue_if_changed()
            self._flush_user_turns()
            return ShoulderTapResult(status="tapped")

        # (4) Re-send the rest as ONE combined turn; the members resolve to Delivered when it commits.
        combined_client_id = self.mint_client_id()
        combined_text = self._combined_text(remaining)
        for entry in remaining:
            entry.combined_client_id = combined_client_id
        try:
            disposition = self.client.submit(combined_text, combined_client_id)
        except CodexAppServerError as exc:
            logger.opt(exception=exc).info("codex ledger: tap resend failed; returning the queue to the composer")
            for entry in remaining:
                entry.state = MessageState.RETURNED
                entry.resend_visible = False
                entry.combined_client_id = None
            self._emit_queue_if_changed()
            self._flush_user_turns()
            # Hand the parked text back to the composer via the response so it is never swallowed (A1a).
            return ShoulderTapResult(status="tapped", returned_block=self._take_returned_block())
        for entry in remaining:
            entry.bound_turn_id = disposition.turn_id
        self._emit_queue_if_changed()
        self._flush_user_turns()
        return ShoulderTapResult(status="tapped")

    # -- reads ------------------------------------------------------------------

    def queued_snapshot(self) -> list[dict[str, Any]]:
        """The wire snapshot of the on-screen chip group, in send order (feeds ``update_queued_messages``).

        Two kinds of chip ride this snapshot, both in ``send_seq`` order:

        * a **Queued** chip (``is_sending=False``) -- a parked steer waiting on the running turn;
        * a **resend** chip (``is_sending=True``) -- a message a shoulder-tap is re-sending as part of
          the combined ``turn/start`` (Fix 3). It stays visible through the interrupt+resend, rendered
          "Sending..." by the frontend, so it never blinks out (A1a); it drops only when the combined
          turn commits (chip removal, then the turn -- A3b).

        ``queued_id`` is the entry's correlation token (not an ``item.id``: codex assigns a parked
        steer no ``item.id`` until it commits -- verified live). The frontend uses it only for
        positional Sending-bubble dedup, so its value need only be stable and unique, which it is."""
        chips = sorted(
            (
                entry
                for entry in self.entries.values()
                if entry.state == MessageState.QUEUED or (entry.state == MessageState.SENDING and entry.resend_visible)
            ),
            key=lambda entry: entry.send_seq,
        )
        return [
            {
                "queued_id": entry.client_id,
                "content": entry.text,
                "timestamp": entry.enqueue_ts,
                "is_sending": entry.state == MessageState.SENDING,
            }
            for entry in chips
        ]

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

    def turn_activity(self) -> ActivityState:
        """The ledger's view of its OWN turn: TOOL_RUNNING / THINKING while one is in flight, IDLE otherwise.

        NOT the agent's activity dot -- that is the transcript tracker's job (see
        ``harnesses/codex/activity.py``). This is the turn-state invariant the conservation
        tests assert the ledger against: open until ``turn/completed`` (the client clears
        ``active_turn_id`` only on the terminal turn frame), not when token-generation stops.
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

    def _queue_user_turn(self, client_id: str | None, epoch_ms: int | None, content: str) -> None:
        """Record a committed user-turn to broadcast after the queue snapshot (A3b), deduped.

        The ``event_id`` is the cross-channel join key (:func:`codex_user_turn_event_id`) so the
        rollout file reader's hydration copy of the same message dedups against this live copy. An
        empty body (nothing to show) or an already-emitted id is a no-op."""
        if not content:
            return
        event_id = codex_user_turn_event_id(client_id, epoch_ms, content)
        if event_id in self.emitted_user_turn_keys:
            return
        self.emitted_user_turn_keys.add(event_id)
        # The commit's own epoch-ms is the authoritative display time; when the frame carried none
        # (the reconcile-only path), fall back to the ledger's injectable clock.
        timestamp = _epoch_ms_to_iso(epoch_ms) if epoch_ms is not None else self.now()
        self._pending_user_turns.append(build_user_turn_event(timestamp, content, event_id))

    def _flush_user_turns(self) -> None:
        """Broadcast the user-turns queued during this notification, in commit order.

        Called AFTER :meth:`_emit_queue_if_changed` so a committed steer's chip is already gone
        before its transcript turn appears (the A3b ordered handoff)."""
        if not self._pending_user_turns:
            return
        pending = self._pending_user_turns
        self._pending_user_turns = []
        if self.on_user_turn is None:
            return
        for event in pending:
            self.on_user_turn(event)

    def _emit_queue_if_changed(self) -> None:
        if self.on_queue_snapshot is None:
            return
        snapshot = self.queued_snapshot()
        if snapshot == self._last_queue_snapshot:
            return
        self._last_queue_snapshot = snapshot
        self.on_queue_snapshot(snapshot)
