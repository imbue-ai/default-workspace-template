"""Dual-channel message conservation for codex (contract Part D, Fix 6).

The codex sibling of ``test_claude_message_lifecycle_conservation.py``, upgraded to the requirement
the plan calls out: drive the ledger's queue snapshot AND the rollout file reader's transcript
emission TOGETHER, in seeded randomized Send / commit / shoulder-tap / interrupt interleavings, and
after EVERY step assert the full live-state law ACROSS BOTH CHANNELS. A ledger-only test (the
sibling ``test_codex_message_lifecycle_conservation.py``) cannot see the cross-channel double-show;
this one can, because it reads *Delivered* from a REAL :class:`CodexSessionWatcher` tailing a rollout
the world writes, and *Queued / Sending / Returned* from the REAL :class:`CodexMessageLedger` over a
scripted app-server stream, and asserts the two channels partition every accepted message.

Two channels, one law:

- **Ledger** (the live edges): Sending / Queued / Returned, keyed by ``clientUserMessageId``, plus the
  live user-turn it emits at commit (chip-removal first, then the turn -- A3b).
- **File reader** (the committed transcript): a message is *Delivered* iff its verbatim text is
  present in a committed user turn on disk. A shoulder-tap re-sends the parked queue as ONE combined
  turn, so its members' texts all land inside that one committed turn -- each is Delivered (present in
  the transcript), which is exactly the contract's delivery = COMMIT (A4).

After every step: exactly-one-state conservation (``delivered + queued + sending + returned == total``,
zero lost, zero ghosts), the ledger's own state agrees with an INDEPENDENT expectation, the delivered
set read from the FILE agrees with that expectation, order is preserved, and returns are the Returned
text in send order (a subsequence of the accepted order). The A3b ordered handoff is asserted in a
controlled corner (chip-removal emitted before the transcript turn, and the ledger's emitted user-turn
shares the file reader's event id, so the two channels dedup rather than double-show). The REQUIRED
interrupt-during-flush corner -- a stop while the queue is non-empty AND a message is in-flight Sending
-- asserts BOTH return together, in send order, as the block prepended on top of the composer.

The tap and the interrupt go through the REAL ``ledger.shoulder_tap()`` / ``ledger.interrupt()`` (Fix
3 / Fix 4), not a modelled shortcut. Fully synchronous and seeded, so a failure replays from the seed.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections import deque
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.codex.ledger import CodexMessageLedger
from imbue.system_interface.harnesses.codex.ledger import MessageState
from imbue.system_interface.harnesses.codex.session_parser import codex_user_turn_event_id
from imbue.system_interface.harnesses.codex.watcher import CodexSessionWatcher
from imbue.system_interface.harnesses.harness_type import HarnessType

pytestmark = pytest.mark.acceptance

_BASE_SEED = 20260812
_ROUND_COUNT = 24
_LIVE_STATES = frozenset({MessageState.SENDING, MessageState.QUEUED})


class _CodexDualChannelWorld:
    """A scripted ``codex app-server`` + REAL ledger, PLUS a rollout file + REAL file reader.

    The world is the fake daemon: it answers the client's RPCs from its own turn/commit state and
    ``push``es notification frames to the ledger. When it commits a ``userMessage`` it ALSO appends the
    matching user-bubble line to the rollout the :class:`CodexSessionWatcher` tails, so the committed
    transcript (the Delivered channel) and the ledger's live edges (Queued/Sending/Returned) are driven
    together. It keeps an INDEPENDENT ``_expected`` map every verify asserts both channels against.
    """

    def __init__(self, root: Path) -> None:
        self._inbound: deque[str] = deque()
        self.sent: list[str] = []
        self._thread_id = "thread-1"
        self._turn_counter = 0
        self._current_turn_id: str | None = None
        self._daemon_committed: list[str] = []
        self._responders = {
            "initialize": self._respond_initialize,
            "thread/start": self._respond_thread_start,
            "turn/start": self._respond_turn_start,
            "turn/steer": self._respond_turn_steer,
            "turn/interrupt": self._respond_ok,
            "thread/read": self._respond_thread_read,
        }
        self.accepted: list[str] = []
        self.text_by_cid: dict[str, str] = {}
        self._expected: dict[str, MessageState] = {}
        # cids a shoulder-tap re-sent: while Sending they render as visible "Sending..." chips, so they
        # ride the chip snapshot (a plain fresh-send Sending does NOT -- the frontend paints its own).
        self._resend_cids: set[str] = set()
        self._returned_handed_off: set[str] = set()
        # When set, the NEXT ``turn/start`` replies with an error (the resend-submit-failure corner).
        self._fail_next_turn_start = False
        self._mint_counter = 0
        self._text_counter = 0
        self.ops: list[str] = []
        # -- the rollout the REAL file reader tails (the Delivered channel) --------------------
        self._agent_state_dir = root / "state"
        sessions_dir = self._agent_state_dir / "plugin" / "codex" / "home" / "sessions"
        sessions_dir.mkdir(parents=True)
        self._rollout_path = sessions_dir / "rollout-1.jsonl"
        self._rollout_path.write_text("")
        (self._agent_state_dir / "codex_transcript_path").write_text(str(self._rollout_path))
        # An epoch-ms base for committed user-bubble timestamps (only ordering matters).
        self._commit_clock = 1_760_000_000_000
        # -- the real client + ledger + file reader over this world ---------------------------
        self.client = CodexAppServerClient(transport=self)
        self.client.initialize("mngr", "0.1")
        self.client.thread_start(cwd="/work")
        # Ordered log of the ledger's two live channels (chip snapshots + emitted user-turns) for A3b.
        self.channel_log: list[tuple[str, Any]] = []
        self.ledger = CodexMessageLedger.build(
            self.client,
            on_queue_snapshot=lambda snapshot: self.channel_log.append(("chip", [c["queued_id"] for c in snapshot])),
            on_user_turn=lambda event: self.channel_log.append(("turn", event["event_id"])),
            mint_client_id=self._mint,
            now=lambda: "2026-08-12T00:00:00Z",
        )
        self.watcher = CodexSessionWatcher.build(self._agent_info(), on_events=lambda _a, _e: None)

    def _agent_info(self) -> AgentInfo:
        return AgentInfo(
            id="codex-dual-agent",
            name="codex-dual-agent",
            state="RUNNING",
            agent_state_dir=self._agent_state_dir,
            claude_config_dir=self._agent_state_dir / "config",
            harness=HarnessType.CODEX,
        )

    # -- AppServerTransport (the scripted daemon) ------------------------------------------

    def send(self, message: str) -> None:
        request = json.loads(message)
        method = request.get("method")
        if "id" not in request:
            return
        self.sent.append(message)
        responder = self._responders.get(method)
        result = responder(request) if responder is not None else {}
        # A responder returning None has pushed its own frame (e.g. an error), so send nothing here.
        if result is None:
            return
        self._push({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def receive(self, timeout: float | None) -> str:
        if not self._inbound:
            raise TimeoutError("no frame available")
        return self._inbound.popleft()

    def close(self) -> None:
        return None

    def _push(self, frame: dict[str, Any]) -> None:
        self._inbound.append(json.dumps(frame))

    def _respond_initialize(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {"userAgent": "mngr", "codexHome": "/home", "platformFamily": "unix", "platformOs": "linux"}

    def _respond_thread_start(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {"thread": {"id": self._thread_id, "status": {"type": "idle"}}}

    def _respond_turn_start(self, request: dict[str, Any]) -> dict[str, Any] | None:
        # Injected failure for the resend-submit-failure corner: reply with an error (and open no turn),
        # so the ledger's combined resend ``submit`` raises. Returns None -- the frame is pushed here.
        if self._fail_next_turn_start:
            self._fail_next_turn_start = False
            self._push({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": "resend boom"}})
            return None
        self._turn_counter += 1
        self._current_turn_id = f"turn-{self._turn_counter}"
        return {"turn": {"id": self._current_turn_id, "status": "inProgress"}}

    def _respond_turn_steer(self, _request: dict[str, Any]) -> dict[str, Any]:
        assert self._current_turn_id is not None, "a steer requires a running turn"
        return {"turnId": self._current_turn_id}

    def _respond_ok(self, _request: dict[str, Any]) -> dict[str, Any]:
        return {}

    def _respond_thread_read(self, _request: dict[str, Any]) -> dict[str, Any]:
        items = [{"type": "userMessage", "clientId": cid} for cid in self._daemon_committed]
        turn_id = self._current_turn_id if self._current_turn_id is not None else "turn-final"
        return {"thread": {"id": self._thread_id, "turns": [{"id": turn_id, "items": items}]}}

    # -- committing a userMessage on BOTH channels -----------------------------------------

    def _commit(self, client_id: str, content: str) -> None:
        """The daemon commits ``client_id``'s userMessage: append it to the rollout (Delivered channel)
        AND push its ``item/completed`` to the ledger (live edge). The two carry the SAME id + content,
        which is what makes them dedup rather than double-show."""
        if client_id not in self._daemon_committed:
            self._daemon_committed.append(client_id)
        self._commit_clock += 1000
        with self._rollout_path.open("a", encoding="utf-8") as handle:
            record = {
                "timestamp": _iso_from_epoch_ms(self._commit_clock),
                "type": "event_msg",
                "payload": {"type": "user_message", "message": content, "client_id": client_id},
            }
            handle.write(json.dumps(record) + "\n")
        self._push(
            {
                "jsonrpc": "2.0",
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "userMessage",
                        "id": f"item-{client_id}",
                        "clientId": client_id,
                        "content": [{"type": "text", "text": content}],
                    },
                    "completedAtMs": self._commit_clock,
                },
            }
        )
        self.client.poll_notifications()

    def _push_turn_completed(self, turn_id: str, status: str) -> None:
        items = [{"type": "userMessage", "clientId": cid} for cid in self._daemon_committed]
        self._push(
            {
                "jsonrpc": "2.0",
                "method": "turn/completed",
                "params": {"turn": {"id": turn_id, "status": status, "itemsView": "full", "items": items}},
            }
        )
        self.client.poll_notifications()

    # -- id / text minting -----------------------------------------------------------------

    def _mint(self) -> str:
        self._mint_counter += 1
        return f"cid-{self._mint_counter:03d}"

    def _new_text(self) -> str:
        self._text_counter += 1
        return f"codex-msg-{self._text_counter:03d}"

    def log(self, op: str) -> None:
        self.ops.append(op)

    def note(self) -> str:
        return "\n".join([f"REPLAY: seed={_BASE_SEED}"] + [f"  {op}" for op in self.ops])

    @property
    def is_idle(self) -> bool:
        return self._current_turn_id is None

    # -- the operations, driving the REAL ledger + file reader -----------------------------

    def user_send(self) -> str:
        was_idle = self.is_idle
        text = self._new_text()
        cid = self.ledger.send(text)
        self.accepted.append(cid)
        self.text_by_cid[cid] = text
        self._expected[cid] = MessageState.SENDING if was_idle else MessageState.QUEUED
        return cid

    def commit_head(self) -> None:
        """Commit the oldest still-live NON-resend message (the turn's next yield boundary)."""
        live = sorted(
            (
                entry
                for entry in self.ledger.entries.values()
                if entry.state in _LIVE_STATES and entry.combined_client_id is None
            ),
            key=lambda entry: entry.send_seq,
        )
        if not live:
            return
        entry = live[0]
        self._commit(entry.client_id, entry.text)
        self._expected[entry.client_id] = MessageState.DELIVERED

    def tap(self) -> None:
        """The REAL shoulder tap: interrupt + re-send the parked queue as ONE combined turn (Fix 3).

        The gate is asserted against the ledger AND the independent expectation. On a real tap the
        parked steers become in-flight (Sending) resend chips -- still visible, not yet Delivered."""
        expected_available = (not self._expects(MessageState.SENDING)) and self._expects(MessageState.QUEUED)
        assert self.ledger.is_tap_available() == expected_available, (
            f"tap gate mismatch (ledger={self.ledger.is_tap_available()} expected={expected_available})\n{self.note()}"
        )
        result = self.ledger.shoulder_tap()
        if not expected_available:
            assert result.status in ("send_in_flight", "no_open_turn"), (
                f"a gated tap must be a benign no-op\n{self.note()}"
            )
            return
        assert result.status == "tapped", f"an available tap must tap, got {result.status!r}\n{self.note()}"
        # A successful resend hands nothing back to the composer (that is only on a resend-submit failure).
        assert result.returned_block == "", (
            f"a successful tap returns no composer block, got {result.returned_block!r}"
        )
        # The interrupt cleared the turn; the combined resend opened a fresh one.
        self._current_turn_id = self.ledger.client.active_turn_id
        # The re-sent messages are now in-flight Sending resend chips (still visible, not Delivered).
        for entry in self.ledger.entries.values():
            if entry.resend_visible and entry.state == MessageState.SENDING:
                self._expected[entry.client_id] = MessageState.SENDING
                self._resend_cids.add(entry.client_id)

    def commit_resend(self) -> None:
        """The combined resend turn commits: every member Delivered via ONE committed turn (Fix 3)."""
        members = sorted(
            (
                entry
                for entry in self.ledger.entries.values()
                if entry.resend_visible and entry.state == MessageState.SENDING
            ),
            key=lambda entry: entry.send_seq,
        )
        if not members:
            return
        combined_id = members[0].combined_client_id
        assert combined_id is not None
        combined_text = "\n\n".join(entry.text for entry in members)
        self._commit(combined_id, combined_text)
        for entry in members:
            self._expected[entry.client_id] = MessageState.DELIVERED

    def stop(self) -> None:
        """The REAL interrupt: one ``turn/interrupt`` + reconcile ALL live entries (Fix 4)."""
        for cid, state in list(self._expected.items()):
            if state in _LIVE_STATES:
                self._expected[cid] = (
                    MessageState.DELIVERED if cid in self._daemon_committed else MessageState.RETURNED
                )
        block = self.ledger.interrupt()
        self._current_turn_id = None
        assert block == self._take_expected_returned_block(), f"interrupt block {block!r} mismatch\n{self.note()}"

    def settle_turn_end(self) -> None:
        if self.is_idle:
            return
        turn_id = self._current_turn_id
        assert turn_id is not None
        for cid, state in list(self._expected.items()):
            if state in _LIVE_STATES:
                self._expected[cid] = (
                    MessageState.DELIVERED if cid in self._daemon_committed else MessageState.RETURNED
                )
        self._push_turn_completed(turn_id, status="completed")
        self._current_turn_id = None

    # -- reading the independent expectation -----------------------------------------------

    def _expects(self, state: MessageState) -> bool:
        return any(value == state for value in self._expected.values())

    def _expected_chip_cids(self) -> list[str]:
        # The on-screen chip group = Queued chips PLUS a shoulder-tap's resend chips (Sending but
        # visible). A plain fresh-send Sending is NOT a chip (the frontend paints its own bubble).
        cids = [
            cid
            for cid, state in self._expected.items()
            if state == MessageState.QUEUED or (state == MessageState.SENDING and cid in self._resend_cids)
        ]
        return sorted(cids, key=self.accepted.index)

    def _expected_returned_block(self) -> str:
        cids = [cid for cid, state in self._expected.items() if state == MessageState.RETURNED]
        cids.sort(key=self.accepted.index)
        return "\n".join(self.text_by_cid[cid] for cid in cids)

    def _take_expected_returned_block(self) -> str:
        cids = [
            cid
            for cid, state in self._expected.items()
            if state == MessageState.RETURNED and cid not in self._returned_handed_off
        ]
        cids.sort(key=self.accepted.index)
        self._returned_handed_off.update(cids)
        return "\n".join(self.text_by_cid[cid] for cid in cids)

    # -- reading the FILE (Delivered) channel ----------------------------------------------

    def _committed_transcript(self) -> str:
        """The concatenation of every committed user turn's text on disk (the Delivered channel)."""
        events = self.watcher.get_all_events()
        return "\n".join(event["content"] for event in events if event.get("type") == "user_message")

    # -- the per-step dual-channel law -----------------------------------------------------

    def verify(self, context: str) -> None:
        note = self.note()
        entries = self.ledger.entries

        assert set(entries) == set(self.accepted), (
            f"ledger keys {sorted(entries)} != accepted {sorted(self.accepted)} ({context})\n{note}"
        )

        transcript = self._committed_transcript()
        counts: Counter[str] = Counter()
        for cid in self.accepted:
            text = self.text_by_cid[cid]
            ledger_state = self.ledger.state_of(cid)
            expected = self._expected[cid]
            assert ledger_state == expected, (
                f"ledger state for {text!r}: {ledger_state} != expected {expected} ({context})\n{note}"
            )
            # The Delivered channel (the file) must agree with the ledger: a Delivered message's text is
            # in the committed transcript; a not-yet-delivered one's text is NOT (no double-show).
            in_transcript = text in transcript
            assert in_transcript == (expected == MessageState.DELIVERED), (
                f"transcript disagrees for {text!r}: in_file={in_transcript} expected_delivered="
                f"{expected == MessageState.DELIVERED} ({context})\n{note}"
            )
            counts[str(expected)] += 1

        # Conservation: the four states partition every accepted message.
        assert sum(counts.values()) == len(self.accepted), (
            f"conservation sum {sum(counts.values())} != total accepted {len(self.accepted)} ({context})\n{note}"
        )

        # Order: the on-screen chip group (Queued + resend Sending) is exactly the live set in send order.
        snapshot = self.ledger.queued_snapshot()
        assert [chip["queued_id"] for chip in snapshot] == self._expected_chip_cids(), (
            f"chip order {[c['queued_id'] for c in snapshot]} != expected {self._expected_chip_cids()} ({context})\n{note}"
        )

        # Returns are the Returned text in send order (a subsequence of the accepted order).
        assert self.ledger.reconcile_returned() == self._expected_returned_block(), (
            f"returned block mismatch ({context})\n{note}"
        )


def _iso_from_epoch_ms(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# =============================================================================
# Required deterministic corners.
# =============================================================================


def _assert_a3b_chip_removed_before_transcript_turn(world: _CodexDualChannelWorld) -> None:
    """A controlled Queued->Delivered: the ledger emits the chip REMOVAL before the transcript turn,
    and its emitted user-turn shares the file reader's event id (so the two channels dedup, A3b)."""
    assert not world.is_idle, "the a3b corner needs an open turn to steer into"
    cid = world.user_send()
    assert world.ledger.state_of(cid) == MessageState.QUEUED

    world.channel_log.clear()
    world._commit(cid, world.text_by_cid[cid])
    world._expected[cid] = MessageState.DELIVERED

    kinds = [kind for kind, _payload in world.channel_log]
    assert "chip" in kinds and "turn" in kinds, f"the commit must emit both channels: {world.channel_log}"
    assert kinds.index("chip") < kinds.index("turn"), (
        f"A3b: the chip removal must be emitted before the transcript turn, got {kinds}"
    )
    # The chip snapshot the ledger emitted no longer carries the message (it left the queue).
    _chip_kind, chip_ids = next((k, p) for k, p in world.channel_log if k == "chip")
    assert cid not in chip_ids
    # The ledger's live user-turn event id equals the file reader's -- so on hydration they dedup.
    _turn_kind, ledger_event_id = next((k, p) for k, p in world.channel_log if k == "turn")
    expected_event_id = codex_user_turn_event_id(cid, None, world.text_by_cid[cid])
    assert ledger_event_id == expected_event_id
    file_event_ids = {e["event_id"] for e in world.watcher.get_all_events() if e.get("type") == "user_message"}
    assert ledger_event_id in file_event_ids, "the ledger's live user-turn must be backed by the file reader's copy"


def _run_stop_with_queue_and_inflight_sending(world: _CodexDualChannelWorld) -> None:
    """The REQUIRED case, driven through a REAL in-flight shoulder-tap resend (not a hand-built entry):
    a stop while the queue is non-empty AND a message is in-flight Sending returns BOTH together, in
    send order, as the block prepended on top of the composer (contract Interrupt).

    The in-flight Sending is produced the way the product produces it -- a tap flips a parked steer to a
    resend-visible Sending chip and opens a fresh combined turn (an entry mid-resend, not yet bound to a
    committed turn) -- which is exactly the unbound in-flight Sending the old turn-scoped reconcile
    missed (Fix 4). A second steer is then parked into that fresh turn, so the stop sees both at once."""
    assert not world.is_idle, "the corner needs an open turn"
    resending = world.user_send()
    assert world.ledger.state_of(resending) == MessageState.QUEUED
    world.tap()
    # The tap made ``resending`` a real in-flight (resend-visible) Sending and opened a fresh turn.
    assert world.ledger.state_of(resending) == MessageState.SENDING
    assert not world.is_idle, "the combined resend opened a fresh turn"
    queued = world.user_send()
    assert world.ledger.state_of(queued) == MessageState.QUEUED
    world.verify("required corner setup")

    for cid, state in list(world._expected.items()):
        if state in _LIVE_STATES:
            world._expected[cid] = MessageState.DELIVERED if cid in world._daemon_committed else MessageState.RETURNED
    block = world.ledger.interrupt()
    world._current_turn_id = None
    expected = world._take_expected_returned_block()
    assert block == expected, f"stop block {block!r} != expected {expected!r}\n{world.note()}"
    returned_lines = block.split("\n")
    assert world.text_by_cid[resending] in returned_lines and world.text_by_cid[queued] in returned_lines, (
        "the in-flight Sending resend AND the queued message must return together"
    )
    assert returned_lines.index(world.text_by_cid[resending]) < returned_lines.index(world.text_by_cid[queued]), (
        "the returned block must be in send order (the earlier in-flight resend before the later queued send)"
    )
    assert world.ledger.state_of(resending) == MessageState.RETURNED
    assert world.ledger.state_of(queued) == MessageState.RETURNED


def _run_tap_resend_submit_failure(world: _CodexDualChannelWorld) -> None:
    """A native tap whose combined resend fails to SUBMIT must not swallow the parked text (A1a, Fix 3):
    every member Returns AND the Returned block is handed back through the tap result for the composer,
    in send order -- the same drain-to-composer hand-off Stop uses."""
    assert not world.is_idle, "the corner needs an open turn to park steers into"
    first = world.user_send()
    second = world.user_send()
    assert [world.ledger.state_of(first), world.ledger.state_of(second)] == [MessageState.QUEUED] * 2
    assert world.ledger.is_tap_available() is True

    world._fail_next_turn_start = True
    result = world.ledger.shoulder_tap()
    assert result.status == "tapped", f"a gated-available tap still taps, got {result.status!r}\n{world.note()}"
    # The combined resend's turn/start errored: both members Return and are handed back for the composer.
    # The hand-off is once-only, so the block is EVERY not-yet-handed-off Returned message in send order
    # (any earlier stray return rides along too) -- mirror that with the world's own take.
    world._expected[first] = MessageState.RETURNED
    world._expected[second] = MessageState.RETURNED
    expected_block = world._take_expected_returned_block()
    assert result.returned_block == expected_block, (
        f"the failed resend must hand the parked text back for the composer, got {result.returned_block!r}"
    )
    returned_lines = result.returned_block.split("\n")
    assert world.text_by_cid[first] in returned_lines and world.text_by_cid[second] in returned_lines, (
        "both parked members must be handed back, never swallowed"
    )
    assert returned_lines.index(world.text_by_cid[first]) < returned_lines.index(world.text_by_cid[second]), (
        "the handed-back block must be in send order"
    )
    # The tap interrupted the running turn (fire-and-forget) and the resend never opened a new one.
    world._current_turn_id = None
    assert world.ledger.state_of(first) == MessageState.RETURNED
    assert world.ledger.state_of(second) == MessageState.RETURNED


def _assert_combined_resend_a3b_chip_removed_before_transcript_turn(world: _CodexDualChannelWorld) -> None:
    """The COMBINED resend's A3b ordering (not only a single steer): when the combined turn commits, the
    ledger emits the chip REMOVAL (all resend members leave the chip group) BEFORE the transcript turn,
    and its emitted user-turn shares the file reader's event id so the two channels dedup rather than
    double-show."""
    assert not world.is_idle, "the corner needs an open turn to steer into"
    first = world.user_send()
    second = world.user_send()
    world.tap()
    assert world.ledger.state_of(first) == MessageState.SENDING
    assert world.ledger.state_of(second) == MessageState.SENDING
    combined_id = world.ledger.entries[first].combined_client_id
    assert combined_id is not None and world.ledger.entries[second].combined_client_id == combined_id

    world.channel_log.clear()
    world.commit_resend()

    kinds = [kind for kind, _payload in world.channel_log]
    assert "chip" in kinds and "turn" in kinds, f"the combined commit must emit both channels: {world.channel_log}"
    assert kinds.index("chip") < kinds.index("turn"), (
        f"A3b: the combined resend's chip removal must be emitted before the transcript turn, got {kinds}"
    )
    # The chip snapshot the ledger emitted no longer carries the resend members (they left the group).
    _chip_kind, chip_ids = next((k, p) for k, p in world.channel_log if k == "chip")
    assert first not in chip_ids and second not in chip_ids
    # The ledger's live user-turn keys on the combined turn's client_id -- the file reader's copy of the
    # same committed turn derives the SAME id, so the two channels dedup on hydration.
    _turn_kind, ledger_event_id = next((k, p) for k, p in world.channel_log if k == "turn")
    assert ledger_event_id == codex_user_turn_event_id(combined_id, None, "")
    file_event_ids = {e["event_id"] for e in world.watcher.get_all_events() if e.get("type") == "user_message"}
    assert ledger_event_id in file_event_ids, (
        "the combined resend's live user-turn must be backed by the file reader's copy"
    )


# =============================================================================
# The seeded storm.
# =============================================================================

_A3B_ROUNDS = frozenset({3, 15})
_REQUIRED_STOP_ROUNDS = frozenset({7, 19})
_COMBINED_A3B_ROUNDS = frozenset({5, 21})
_RESEND_FAIL_ROUNDS = frozenset({9, 17})


@pytest.mark.timeout(120, func_only=False)
def test_codex_message_lifecycle_conserves_across_both_channels(tmp_path: Path) -> None:
    """Seeded Send / commit / shoulder-tap / interrupt interleavings; the dual-channel law per step."""
    world = _CodexDualChannelWorld(tmp_path)
    for round_index in range(_ROUND_COUNT):
        rng = random.Random(_BASE_SEED + round_index)
        world.log(f"round {round_index}:")

        if world.is_idle:
            world.log("  kickoff-send")
            world.user_send()
            world.verify(f"round {round_index} kickoff-send")
            world.log("  kickoff-deliver")
            world.commit_head()
            world.verify(f"round {round_index} kickoff-deliver")

        if round_index in _A3B_ROUNDS:
            world.log("  a3b")
            _assert_a3b_chip_removed_before_transcript_turn(world)
            world.verify(f"round {round_index} a3b")
        if round_index in _REQUIRED_STOP_ROUNDS:
            world.log("  stop-with-queue-and-inflight-sending")
            _run_stop_with_queue_and_inflight_sending(world)
            world.verify(f"round {round_index} required-stop")
        if round_index in _COMBINED_A3B_ROUNDS:
            world.log("  combined-resend-a3b")
            _assert_combined_resend_a3b_chip_removed_before_transcript_turn(world)
            world.verify(f"round {round_index} combined-resend-a3b")
        if round_index in _RESEND_FAIL_ROUNDS:
            world.log("  tap-resend-submit-failure")
            _run_tap_resend_submit_failure(world)
            world.verify(f"round {round_index} tap-resend-failure")

        for _op_index in range(rng.randint(2, 4)):
            op = rng.choice(("send", "send", "commit", "tap", "commit_resend", "stop"))
            world.log(f"  {op}")
            if op == "send":
                world.user_send()
            elif op == "commit":
                world.commit_head()
            elif op == "tap":
                world.tap()
            elif op == "commit_resend":
                world.commit_resend()
            else:
                world.stop()
            world.verify(f"round {round_index} op {op}")

        world.log("  settle")
        world.settle_turn_end()
        world.verify(f"round {round_index} settle")

    # Final cross-channel partition: every accepted message is Delivered-on-disk XOR live-in-the-ledger.
    transcript = world._committed_transcript()
    for cid in world.accepted:
        text = world.text_by_cid[cid]
        state = world.ledger.state_of(cid)
        in_file = text in transcript
        assert in_file == (state == MessageState.DELIVERED), f"final channel disagreement for {text!r}: {state}"
