"""Seeded message-conservation storms for the three harness stop/flush executors (invariant U1).

THE PROPERTY (docs/design/harness-message-lifecycle-contract.md A1, U1): across any
interleaving of send / park / stop / flush / restart, every accepted message ends in EXACTLY one
terminal state -- delivered (a user turn on disk), or returned-to-composer (rides a captured stop
block) -- never silently lost and never duplicated as a ghost re-queue after a replay. The one
designed-in exception is the shipped "slow-send corner" (contract E2): a send still holding
``message.lock`` past the bounded wait when a stop hammers is *stopped, never runs* -- the ledger admits that message as
KILLED, and only when the storm deliberately staged such a send.

Each storm drives the REAL executor under test -- claude's tap + stop executors over a REAL
:class:`ClaudeSessionWatcher` reading real session JSONL, and pi's flush/retract inbox writers --
against seeded rounds of interleaved operations, with REAL
``message.lock`` flock contention: an in-flight send takes the same exclusive flock mngr's send
holds (the ``server_test`` in-flight pattern) on a separate open file description BEFORE the
executor starts its bounded acquire, and a timer thread completes the send (park-then-release)
mid-acquire. What the executors cannot see -- the harness process consuming its inbox / control
file / cancel chord -- is simulated by a scripted world that replays the REAL bytes the executor
wrote, in file order, exactly as the extension / patched binary / claude would.

The op sequence is derived purely from ``_BASE_SEED`` (per-round ``random.Random``), so a failure
is replayable: every assertion message carries the seed and the full cumulative op log. Timing
races inside one op (who wins the flock) are made deterministic by construction -- the in-flight
send always holds the lock before the executor contends -- so a replay exercises the same branch.

The conservation ledger is verified after every round. Rounds listed in ``_STOP_HAMMER_ROUNDS`` /
``_FLUSH_BLOCKED_ROUNDS`` stage the expensive slow-send corner (the pi/codex executors wait the
real ``STOP_LOCK_WAIT_SECONDS`` bound); all other contention resolves fast, keeping each storm
well under the runtime budget.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.system_interface.harnesses.antigravity.tap import AntigravityAtomicShoulderTap
from imbue.system_interface.harnesses.antigravity.tap import AntigravityInterruptToComposer
from imbue.system_interface.harnesses.antigravity.turn_state import drop_turn_state
from imbue.system_interface.harnesses.antigravity.turn_state import get_turn_state
from imbue.system_interface.harnesses.claude.session_parser import INTERRUPT_SENTINEL_TEXT
from imbue.system_interface.harnesses.claude.tap import ClaudeTapStatus
from imbue.system_interface.harnesses.claude.tap import execute_claude_shoulder_tap
from imbue.system_interface.harnesses.claude.tap import execute_claude_stop_to_composer
from imbue.system_interface.harnesses.claude.watcher import ClaudeSessionWatcher
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.system_interface.harnesses.interrupt import restart_drain
from imbue.system_interface.harnesses.interrupt import try_hold_message_lock
from imbue.system_interface.harnesses.pi_coding.inbox import PI_INBOX_NAME
from imbue.system_interface.harnesses.pi_coding.inbox import PI_INTERRUPT_KEY
from imbue.system_interface.harnesses.pi_coding.inbox import PI_RETRACT_KEY
from imbue.system_interface.harnesses.pi_coding.model import PiFlushTapStatus
from imbue.system_interface.harnesses.pi_coding.model import PiInterruptToComposer
from imbue.system_interface.harnesses.pi_coding.model import flush_pi_queue_atomic
from imbue.system_interface.harnesses.session_watcher import AgentSessionWatcher
from imbue.system_interface.harnesses.session_watcher import OnEventsCallback
from imbue.system_interface.testing import agent_message_lock

# One seed drives every storm; a failure report carries it plus the op log for replay.
_BASE_SEED = 20260810
_ROUND_COUNT = 30

# Rounds that stage the slow-send corner (a send holding ``message.lock`` past the executor's
# bounded wait). For pi/codex the executor waits the real STOP_LOCK_WAIT_SECONDS (2.0s), so these
# are kept to a fixed, small set to bound the storm's runtime; claude's bounded wait is injected
# small, so its round schedule lets the rng stage slow sends freely.
_STOP_HAMMER_ROUNDS = frozenset({7, 19})

# Worker ticks allowed at the end of a round for the queue to empty. More than one because a
# claim may be outstanding when the round ends; far fewer than the attempt ceiling, so a gate
# that never releases still fails.
_SETTLE_TICKS = 4
_FLUSH_BLOCKED_ROUNDS = frozenset({13})

# How long an in-flight send holds the lock: FAST resolves within the executor's bounded wait
# (real contention, native path); SLOW outlives the pi/codex 2.0s bound (the hammer corner).
_FAST_HOLD_SECONDS = 0.15
_SLOW_HOLD_SECONDS = 2.5

# Claude's executor takes an injected bounded lock, so its slow corner is cheap.
_CLAUDE_LOCK_WAIT_SECONDS = 0.3
_CLAUDE_SLOW_HOLD_SECONDS = 0.8

# Logical clock base for the claude world's on-disk timestamps and marker mtimes (an arbitrary
# past epoch; only the ordering matters).
_CLOCK_BASE = 1_600_000_000.0

_SEND_MODE_NONE = "none"
_SEND_MODE_FAST = "inflight-fast"
_SEND_MODE_SLOW = "inflight-slow"


def _iso_timestamp(epoch_seconds: float) -> str:
    """An ISO-8601 UTC timestamp (claude session-record shape) for a logical epoch second."""
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_seconds))
    millis = int(round((epoch_seconds % 1.0) * 1000.0))
    return f"{base}.{millis:03d}Z"


class _Ledger:
    """The conservation ledger: every accepted message's terminal state, verified per round.

    ``accepted`` records every message a send op produced; ``delivered`` / ``returned`` /
    ``killed`` record terminal states as ground truth observes them (the simulated harness's
    commits and discards, the executors' returned blocks, the staged slow-send deaths).
    ``killable`` is the set of messages the storm deliberately staged as the slow-send corner --
    the only ones allowed to die. ``ops`` is the cumulative op log for the replay report.
    """

    def __init__(self, storm_name: str) -> None:
        self.storm_name = storm_name
        self.accepted: list[str] = []
        self.delivered: list[str] = []
        self.returned: list[str] = []
        self.killed: list[str] = []
        self.killable: set[str] = set()
        self.ops: list[str] = []

    def log(self, op: str) -> None:
        self.ops.append(op)

    def replay_note(self) -> str:
        lines = [f"REPLAY {self.storm_name}: seed={_BASE_SEED}"] + [f"  {op}" for op in self.ops]
        return "\n".join(lines)

    def verify(self) -> None:
        note = self.replay_note()
        terminal_counts = Counter(self.delivered + self.returned + self.killed)
        accepted_set = set(self.accepted)
        lost = [text for text in self.accepted if terminal_counts.get(text, 0) == 0]
        assert not lost, f"LOST messages (accepted, but neither delivered nor returned nor killed): {lost}\n{note}"
        duplicated = {text: count for text, count in terminal_counts.items() if count > 1}
        assert not duplicated, f"DUPLICATED messages (more than one terminal state -- a ghost): {duplicated}\n{note}"
        strangers = [text for text in terminal_counts if text not in accepted_set]
        assert not strangers, f"messages reached a terminal state without ever being accepted: {strangers}\n{note}"
        bad_kills = [text for text in self.killed if text not in self.killable]
        assert not bad_kills, f"SILENTLY LOST (killed without a staged in-flight hammer): {bad_kills}\n{note}"


class _InFlightSend:
    """A REAL in-flight mngr send: holds the agent's ``message.lock`` flock, then parks-and-releases.

    The exclusive flock is taken on a separate open file description IN THE DRIVER THREAD, before
    the executor under test starts its bounded acquire -- so the contention ordering is
    deterministic. A timer thread then runs ``deliver`` (the send's durable park) while still
    holding the lock and releases it, exactly the order mngr's send follows. ``join`` waits for
    the release so the driver can settle deterministically.
    """

    def __init__(self, agent_state_dir: Path, hold_seconds: float, deliver: Callable[[], None]) -> None:
        lock_path = agent_state_dir / MESSAGE_LOCK_FILENAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(lock_path, "w")
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX)
        self._deliver = deliver
        self._released = threading.Event()
        self._timer = threading.Timer(hold_seconds, self._complete)
        self._timer.start()

    def _complete(self) -> None:
        try:
            self._deliver()
        finally:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._released.set()

    def join(self) -> None:
        is_released = self._released.wait(timeout=30.0)
        assert is_released, "the staged in-flight send never released message.lock"


class _StormWatcherBase(AgentSessionWatcher):
    """A minimal concrete watcher for the pi storm fakes.

    The transcript surface is inert (the executors under test never read it); subclasses override
    only the queued-message surface. Constructed directly by the storms, never via ``build``.
    """

    @classmethod
    def build(cls, agent_info: AgentInfo, on_events: OnEventsCallback) -> "AgentSessionWatcher":
        pytest.fail("storm watchers are constructed directly, not via build")

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_tail_events(self, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_backfill_events(
        self, before_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_forward_events(
        self, after_event_id: str, limit: int, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return []

    def get_events_at_offset(self, offset: int, limit: int, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_event_offset(self, event_id: str, session_id: str | None = None) -> int:
        return -1

    def get_total_event_count(self, session_id: str | None = None) -> int:
        return 0

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return True


# =============================================================================
# pi: the flush/retract inbox writers against the real pi_inbox + real lock
# =============================================================================


class _PiWorld:
    """Ground truth for the pi storm: the REAL ``pi_inbox`` file plus a simulated extension.

    Sends append JSON-string lines under the real ``message.lock`` (mngr's shape); the executors
    under test append their sentinel object lines under the same lock. ``consume`` replays new
    inbox lines strictly in file order the way the lifecycle extension drains them: a string
    parks as a steer, a flush sentinel commits the parked set, a retract sentinel discards it.
    Conservation therefore reads directly off the byte order the real lock produced.
    """

    def __init__(self, agent_state_dir: Path, ledger: _Ledger) -> None:
        self.agent_state_dir = agent_state_dir
        self.inbox_path = agent_state_dir / PI_INBOX_NAME
        self.ledger = ledger
        self.parked: list[str] = []
        self.last_discarded: list[str] = []
        self.restart_count = 0
        self._consumed_line_count = 0
        self._message_counter = 0

    def new_text(self) -> str:
        self._message_counter += 1
        return f"pi-msg-{self._message_counter:03d}"

    def _read_lines(self) -> list[str]:
        if not self.inbox_path.exists():
            return []
        return self.inbox_path.read_text(encoding="utf-8").splitlines()

    def append_message_line(self, text: str) -> None:
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.inbox_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(text) + "\n")

    def send_now(self, text: str) -> None:
        """A completed send: the inbox append under the real message lock, then release."""
        self.ledger.accepted.append(text)
        with agent_message_lock(self.agent_state_dir):
            self.append_message_line(text)

    def begin_inflight_send(self, text: str, hold_seconds: float) -> _InFlightSend:
        self.ledger.accepted.append(text)
        return _InFlightSend(self.agent_state_dir, hold_seconds, lambda: self.append_message_line(text))

    def count_sentinel_lines(self) -> int:
        count = 0
        for line in self._read_lines():
            if not isinstance(json.loads(line), str):
                count += 1
        return count

    def peek_parked(self) -> list[str]:
        """The parked set the live inbox replay derives right now (the real pi mirror semantics)."""
        parked = list(self.parked)
        for line in self._read_lines()[self._consumed_line_count :]:
            content = json.loads(line)
            if isinstance(content, str):
                parked.append(content)
            else:
                parked = []
        return parked

    def consume(self) -> None:
        """The extension's in-order drain of every not-yet-consumed inbox line."""
        lines = self._read_lines()
        for line in lines[self._consumed_line_count :]:
            content = json.loads(line)
            if isinstance(content, str):
                self.parked.append(content)
            elif content.get(PI_INTERRUPT_KEY) is True:
                self.ledger.delivered.extend(self.parked)
                self.parked = []
            elif content.get(PI_RETRACT_KEY) is True:
                self.last_discarded = list(self.parked)
                self.parked = []
            else:
                pytest.fail(f"unrecognized pi inbox line: {line!r}\n{self.ledger.replay_note()}")
        self._consumed_line_count = len(lines)

    def restart_process(self) -> tuple[bool, str]:
        """The base drain's SIGKILL-relaunch boundary (recorded; the epoch settles in the driver)."""
        self.restart_count += 1
        return (True, "ok")

    def truncate_epoch(self) -> None:
        """The relaunched extension truncates the inbox at load: still-parked lines die.

        A parked message already handed back to the composer stays RETURNED; anything else
        parked at the boundary is KILLED (the staged slow-send corner is the only legal case,
        which ``_Ledger.verify`` enforces).
        """
        already_returned = set(self.ledger.returned)
        for text in self.parked:
            if text not in already_returned:
                self.ledger.killed.append(text)
        self.parked = []
        self.inbox_path.write_text("")
        self._consumed_line_count = 0

    def settle_turn_end(self) -> None:
        """Natural turn end: the extension commits every still-parked steer."""
        self.consume()
        self.ledger.delivered.extend(self.parked)
        self.parked = []


class _PiStormWatcher(_StormWatcherBase):
    """The pi queue mirror over the REAL inbox file: the live replay's parked set."""

    def __init__(self, world: _PiWorld) -> None:
        self._world = world
        self.clear_calls = 0

    def get_queued_block(self) -> str:
        return "\n".join(self._world.peek_parked())

    def clear_queue(self) -> None:
        self.clear_calls += 1


def _block_texts(block: str) -> list[str]:
    return block.split("\n") if block else []


def _pi_agent_info(agent_state_dir: Path) -> AgentInfo:
    return AgentInfo(
        id="pi-storm-agent",
        name="pi-storm-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "unused",
        harness=HarnessType.PI_CODING,
    )


def _stage_pi_send(world: _PiWorld, send_mode: str) -> _InFlightSend | None:
    if send_mode == _SEND_MODE_NONE:
        return None
    text = world.new_text()
    if send_mode == _SEND_MODE_SLOW:
        world.ledger.killable.add(text)
        return world.begin_inflight_send(text, _SLOW_HOLD_SECONDS)
    return world.begin_inflight_send(text, _FAST_HOLD_SECONDS)


def _run_pi_stop(world: _PiWorld, watcher: _PiStormWatcher, agent_info: AgentInfo, send_mode: str) -> None:
    note = world.ledger.replay_note
    restarts_before = world.restart_count
    sentinels_before = world.count_sentinel_lines()
    block_before = world.peek_parked()
    sender = _stage_pi_send(world, send_mode)
    interrupter = PiInterruptToComposer.build(agent_info)
    block = interrupter.drain_to_composer(watcher, world.restart_process, lambda: None, lambda: True, lambda: "")
    if sender is not None:
        sender.join()
    returned = _block_texts(block)
    if send_mode == _SEND_MODE_SLOW and world.restart_count == restarts_before + 1:
        # The hammer corner (the staged outcome): the lock stayed held past the bounded wait, so
        # no sentinel was written, the process was restarted, and the block is the pre-drain
        # parked set. The in-flight line lands after the capture and dies at the epoch boundary
        # (KILLED, staged). On a heavily stalled machine the holder can release just inside the
        # wait instead -- then the native branch below applies and conservation holds without a
        # kill (the ledger allows but never requires a staged kill).
        assert world.count_sentinel_lines() == sentinels_before, f"no sentinel may be written unordered\n{note()}"
        assert returned == block_before, f"hammer block {returned} != parked-at-capture {block_before}\n{note()}"
        world.ledger.returned.extend(returned)
        world.consume()
        world.truncate_epoch()
        return
    # Native path: the retract sentinel was ordered after any in-flight send under the lock, so
    # the extension's discard set must be EXACTLY the returned block -- the conservation crux.
    assert world.restart_count == restarts_before, f"the native pi stop must not restart\n{note()}"
    world.consume()
    assert world.last_discarded == returned, (
        f"pi retract discarded {world.last_discarded} but the composer got {returned}\n{note()}"
    )
    world.ledger.returned.extend(returned)


def _run_pi_flush(world: _PiWorld, send_mode: str) -> None:
    note = world.ledger.replay_note
    sentinels_before = world.count_sentinel_lines()
    sender = _stage_pi_send(world, send_mode)
    status = flush_pi_queue_atomic(world.agent_state_dir)
    if sender is not None:
        sender.join()
    if send_mode == _SEND_MODE_SLOW:
        if status == PiFlushTapStatus.SEND_IN_FLIGHT:
            # The staged outcome: the lock stayed held past the bounded wait, so nothing was
            # written -- the flush refused instead of racing the send. The staged message was
            # NOT killed (no hammer): it parks once the send completes and is delivered at
            # settle, so un-stage it. (On a heavily stalled machine the holder can release
            # just inside the wait; the flush then legitimately taps -- the other branch.)
            assert world.count_sentinel_lines() == sentinels_before, f"a refused flush writes nothing\n{note()}"
            world.consume()
            world.ledger.killable.difference_update(world.parked)
            return
        assert status == PiFlushTapStatus.TAPPED, f"unexpected pi flush status {status}\n{note()}"
        world.consume()
        world.ledger.killable.difference_update(world.ledger.delivered)
        return
    assert status == PiFlushTapStatus.TAPPED, f"unexpected pi flush status {status}\n{note()}"
    world.consume()


@pytest.mark.timeout(120)
def test_pi_conservation_storm_flush_and_retract_writers(tmp_path: Path) -> None:
    """N seeded rounds of interleaved pi sends / stops / flushes under real lock contention."""
    ledger = _Ledger("pi")
    world = _PiWorld(tmp_path, ledger)
    watcher = _PiStormWatcher(world)
    agent_info = _pi_agent_info(tmp_path)
    for round_index in range(_ROUND_COUNT):
        rng = random.Random(_BASE_SEED + round_index)
        ledger.log(f"round {round_index}:")
        op_count = rng.randint(2, 4)
        for op_index in range(op_count):
            if round_index in _STOP_HAMMER_ROUNDS and op_index == 0:
                op = "stop:" + _SEND_MODE_SLOW
            elif round_index in _FLUSH_BLOCKED_ROUNDS and op_index == 0:
                op = "flush:" + _SEND_MODE_SLOW
            else:
                op = rng.choice(
                    (
                        "send",
                        "send",
                        "stop:" + _SEND_MODE_NONE,
                        "stop:" + _SEND_MODE_FAST,
                        "flush:" + _SEND_MODE_NONE,
                        "flush:" + _SEND_MODE_FAST,
                    )
                )
            ledger.log(f"  {op}")
            if op == "send":
                world.send_now(world.new_text())
            elif op.startswith("stop:"):
                _run_pi_stop(world, watcher, agent_info, op.split(":", 1)[1])
            elif op.startswith("flush:"):
                _run_pi_flush(world, op.split(":", 1)[1])
            else:
                pytest.fail(f"unknown pi op {op}")
        world.settle_turn_end()
        ledger.verify()


# =============================================================================
# claude: the tap + stop executors over a REAL ClaudeSessionWatcher and real session JSONL
# =============================================================================


class _ClaudeWorld:
    """Ground truth for the claude storm: a REAL session JSONL a simulated claude appends to.

    The REAL :class:`ClaudeSessionWatcher` (unstarted; synchronous reads) derives the queue
    mirror from the on-disk queue-operation ledger, exactly as production does -- including the
    process-epoch scoping of replays. The world plays claude's side: sends append enqueue
    records (idle sends deliver a user turn directly), the cancel chord aborts or
    flushes-through per the verified 2.1.207 behavior, restarts bump the
    ``claude_process_started`` marker so dead-epoch enqueues dangle, and natural turn end
    auto-flushes the parked queue.
    """

    def __init__(self, root: Path, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.agent_state_dir = root / "state"
        self.agent_state_dir.mkdir()
        self.claude_config_dir = root / "config"
        session_dir = self.claude_config_dir / "projects" / "storm"
        session_dir.mkdir(parents=True)
        self.session_id = "storm-session"
        self.session_file = session_dir / f"{self.session_id}.jsonl"
        self.session_file.write_text("")
        (self.agent_state_dir / "claude_session_id_history").write_text(f"{self.session_id}\n")
        self.keybindings_path = self.claude_config_dir / "keybindings.json"
        self.keybindings_path.write_text(
            json.dumps({"bindings": [{"context": "Chat", "bindings": {"meta+q": "chat:cancel"}}]})
        )
        self.clock = _CLOCK_BASE
        os.utime(self.keybindings_path, (self.clock, self.clock))
        self.process_marker = self.agent_state_dir / "claude_process_started"
        self.process_marker.write_text("")
        self.clock += 5.0
        os.utime(self.process_marker, (self.clock, self.clock))
        self.active_marker = self.agent_state_dir / "active"
        self.parked: list[str] = []
        self.turn_open = False
        self.restart_count = 0
        self.dead_at_restart: list[str] = []
        self._generation = 0
        self._uuid_counter = 0
        self._message_counter = 0
        self.watcher = self._build_watcher()

    def _build_watcher(self) -> ClaudeSessionWatcher:
        agent_info = AgentInfo(
            id="claude-storm-agent",
            name="claude-storm-agent",
            state="RUNNING",
            agent_state_dir=self.agent_state_dir,
            claude_config_dir=self.claude_config_dir,
            harness=HarnessType.CLAUDE,
        )
        watcher = ClaudeSessionWatcher.build(agent_info, on_events=lambda _agent_id, _events: None)
        return watcher

    def new_text(self) -> str:
        self._message_counter += 1
        return f"claude-msg-{self._message_counter:03d}"

    def _tick(self) -> float:
        self.clock += 1.0
        return self.clock

    def _next_uuid(self) -> str:
        self._uuid_counter += 1
        return f"storm-uuid-{self._uuid_counter:04d}"

    def _append(self, record: dict[str, Any]) -> None:
        with self.session_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _append_enqueue(self, text: str, timestamp: str) -> None:
        self._append(
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": text,
                "timestamp": timestamp,
                "sessionId": self.session_id,
            }
        )

    def _append_leave(self) -> None:
        self._append({"type": "queue-operation", "operation": "dequeue", "sessionId": self.session_id})

    def _append_user(self, text: str) -> None:
        self._append(
            {
                "type": "user",
                "uuid": self._next_uuid(),
                "timestamp": _iso_timestamp(self._tick()),
                "sessionId": self.session_id,
                "message": {"role": "user", "content": text},
            }
        )

    def _append_assistant(self, text: str) -> None:
        self._append(
            {
                "type": "assistant",
                "uuid": self._next_uuid(),
                "timestamp": _iso_timestamp(self._tick()),
                "sessionId": self.session_id,
                "message": {"role": "assistant", "model": "storm-model", "content": [{"type": "text", "text": text}]},
            }
        )

    def open_turn(self) -> None:
        self.active_marker.write_text("")
        self.turn_open = True

    def send_now(self, text: str) -> None:
        """A completed send under the real lock: mid-turn parks (enqueue record); idle delivers."""
        self.ledger.accepted.append(text)
        with agent_message_lock(self.agent_state_dir):
            self._deliver_send(text, _iso_timestamp(self._tick()), self._generation)

    def _deliver_send(self, text: str, timestamp: str, generation: int) -> None:
        if self._generation != generation:
            # The paste raced the SIGKILL: claude wrote the enqueue record just before dying, so
            # a dangling dead-epoch enqueue is on disk and the message never runs (staged corner).
            self._append_enqueue(text, timestamp)
            self.ledger.killed.append(text)
        elif self.turn_open:
            self._append_enqueue(text, timestamp)
            self.parked.append(text)
        else:
            self._append_user(text)
            self.ledger.delivered.append(text)
            self.open_turn()

    def begin_inflight_send(self, text: str, hold_seconds: float) -> _InFlightSend:
        self.ledger.accepted.append(text)
        timestamp = _iso_timestamp(self._tick())
        generation = self._generation
        return _InFlightSend(
            self.agent_state_dir, hold_seconds, lambda: self._deliver_send(text, timestamp, generation)
        )

    def press_chord_stop(self) -> bool:
        """The cancel chord on an EMPTY queue: a pure abort. The interrupt sentinel lands; the
        turn dies; the ``active`` marker STAYS stranded (claude fires no hook on interrupt)."""
        self._append_user(INTERRUPT_SENTINEL_TEXT)
        self.turn_open = False
        return True

    def press_chord_flush(self) -> bool:
        """The cancel chord on a NONEMPTY queue: the verified flush-through. The parked queue
        commits (leave + user turn each) as a fresh merged turn that keeps running."""
        self._append_user(INTERRUPT_SENTINEL_TEXT)
        self._commit_parked()
        return True

    def _commit_parked(self) -> None:
        for text in self.parked:
            self._append_leave()
            self._append_user(text)
            self.ledger.delivered.append(text)
        self.parked = []

    def mark_idle(self) -> None:
        self.active_marker.unlink(missing_ok=True)

    def restart_process(self) -> tuple[bool, str]:
        """The SIGKILL-relaunch: parked enqueues dangle (their epoch died), the process marker's
        mtime advances past them, and the relaunched process is idle."""
        self.restart_count += 1
        self._generation += 1
        self.dead_at_restart = list(self.parked)
        self.parked = []
        self.turn_open = False
        self.active_marker.unlink(missing_ok=True)
        restart_time = self._tick()
        os.utime(self.process_marker, (restart_time, restart_time))
        return (True, "ok")

    def assert_mirror_matches(self, context: str) -> None:
        """Contract A over the real watcher: the derived mirror equals the live parked set."""
        self.watcher.get_all_events()
        snapshot = [entry["content"] for entry in self.watcher.get_queued_messages()]
        assert snapshot == self.parked, (
            f"mirror {snapshot} != live parked {self.parked} ({context})\n{self.ledger.replay_note()}"
        )

    def swap_backend_watcher(self) -> None:
        """A backend restart: a FRESH watcher replays the whole ledger from byte zero. Dead-epoch
        enqueues (returned or killed messages) must not re-derive as ghosts (U6)."""
        self.watcher = self._build_watcher()
        self.assert_mirror_matches("after backend-restart full replay")

    def settle_turn_end(self) -> None:
        """Natural turn end: the auto-flush commits the parked queue, the Stop hook settles."""
        if self.parked:
            self._commit_parked()
        if self.turn_open:
            self._append_assistant("ok")
            self.turn_open = False
        self.active_marker.unlink(missing_ok=True)


def _run_claude_stop(world: _ClaudeWorld, send_mode: str) -> None:
    note = world.ledger.replay_note
    restarts_before = world.restart_count
    parked_before = list(world.parked)
    turn_open_before = world.turn_open
    sender: _InFlightSend | None = None
    inflight_text: str | None = None
    if send_mode != _SEND_MODE_NONE:
        inflight_text = world.new_text()
        hold = _CLAUDE_SLOW_HOLD_SECONDS if send_mode == _SEND_MODE_SLOW else _FAST_HOLD_SECONDS
        if send_mode == _SEND_MODE_SLOW:
            world.ledger.killable.add(inflight_text)
        sender = world.begin_inflight_send(inflight_text, hold)
    agent_info = AgentInfo(
        id="claude-storm-agent",
        name="claude-storm-agent",
        state="RUNNING",
        agent_state_dir=world.agent_state_dir,
        claude_config_dir=world.claude_config_dir,
        harness=HarnessType.CLAUDE,
    )
    block = execute_claude_stop_to_composer(
        agent_state_dir=world.agent_state_dir,
        keybindings_path=world.keybindings_path,
        watcher=world.watcher,
        press_chord=world.press_chord_stop,
        mark_idle=world.mark_idle,
        restart_drain_to_base=lambda: restart_drain(agent_info, world.watcher, world.restart_process, lambda: None),
        try_message_lock=lambda: try_hold_message_lock(world.agent_state_dir, wait_seconds=_CLAUDE_LOCK_WAIT_SECONDS),
    )
    if sender is not None:
        sender.join()
    returned = _block_texts(block)
    world.ledger.returned.extend(returned)
    if not turn_open_before and not parked_before:
        # Nothing running and nothing queued when the stop dispatched: a pure no-op -- the
        # executor returns before ever taking the lock, so a staged in-flight send simply
        # completes afterwards as a fresh idle send (delivered; nothing is staged to die).
        assert block == "", f"an idle claude stop must return an empty block\n{note()}"
        assert world.restart_count == restarts_before, f"an idle claude stop must not restart\n{note()}"
        if inflight_text is not None:
            world.ledger.killable.discard(inflight_text)
        return
    if send_mode == _SEND_MODE_SLOW:
        # The bounded wait expired: the hammer fell on a best-effort re-capture, and the
        # in-flight enqueue landed after it, in the dead epoch -- stopped, never runs (staged).
        # (On a heavily stalled machine the holder can release just inside the wait; the base
        # drain then captures the just-parked message under the lock and it rides the block --
        # conservation holds either way, so both capture shapes are accepted.)
        assert world.restart_count == restarts_before + 1, f"the blocked claude stop must restart\n{note()}"
        expected_blocks = (parked_before, parked_before + [inflight_text])
        assert returned in expected_blocks, f"hammer block {returned} not in {expected_blocks}\n{note()}"
        return
    if parked_before or send_mode == _SEND_MODE_FAST:
        # The base restart-drain, captured under the lock: everything parked -- including a
        # message that parked mid-stop -- rides the returned block (the conservation crux).
        assert world.restart_count == restarts_before + 1, f"a nonempty claude stop must restart\n{note()}"
        assert returned == world.dead_at_restart, (
            f"claude drain returned {returned} but the SIGKILL took {world.dead_at_restart}\n{note()}"
        )
        if send_mode == _SEND_MODE_FAST and inflight_text is not None:
            assert inflight_text in returned, f"the mid-stop parked send must ride the block\n{note()}"
        return
    # Empty queue, open turn, no contention: the chord path. The abort was confirmed by the
    # sentinel and the stranded ``active`` marker cleared; nothing restarted, nothing returned.
    assert block == "", f"the chord stop must return an empty block\n{note()}"
    assert world.restart_count == restarts_before, f"the chord stop must not restart\n{note()}"
    assert not world.active_marker.exists(), f"the confirmed abort must clear the active marker\n{note()}"
    assert not world.turn_open, f"the chord stop must end the turn\n{note()}"


def _run_claude_tap(world: _ClaudeWorld) -> None:
    note = world.ledger.replay_note
    parked_before = list(world.parked)
    turn_open_before = world.turn_open
    result = execute_claude_shoulder_tap(
        agent_state_dir=world.agent_state_dir,
        keybindings_path=world.keybindings_path,
        watcher=world.watcher,
        press_chord=world.press_chord_flush,
        send_recovery=lambda _text: True,
        try_message_lock=lambda: try_hold_message_lock(world.agent_state_dir, wait_seconds=_CLAUDE_LOCK_WAIT_SECONDS),
    )
    if not parked_before:
        assert result.status == ClaudeTapStatus.NOTHING_QUEUED, f"an empty tap must no-op ({result})\n{note()}"
        return
    if not turn_open_before:
        assert result.status == ClaudeTapStatus.NO_OPEN_TURN, f"a tap with no turn must no-op ({result})\n{note()}"
        return
    assert result.status == ClaudeTapStatus.TAPPED, f"unexpected tap status {result}\n{note()}"
    assert world.parked == [], f"a tapped flush must commit the whole queue\n{note()}"


@pytest.mark.timeout(120)
def test_claude_conservation_storm_tap_and_stop_executors(tmp_path: Path) -> None:
    """N seeded rounds of claude sends / taps / stops / restarts over the REAL session watcher."""
    ledger = _Ledger("claude")
    world = _ClaudeWorld(tmp_path, ledger)
    for round_index in range(_ROUND_COUNT):
        rng = random.Random(_BASE_SEED + round_index)
        ledger.log(f"round {round_index}:")
        if not world.turn_open:
            ledger.log("  kickoff-send")
            world.send_now(world.new_text())
        op_count = rng.randint(2, 4)
        for _op_index in range(op_count):
            op = rng.choice(
                (
                    "send",
                    "send",
                    "stop:" + _SEND_MODE_NONE,
                    "stop:" + _SEND_MODE_FAST,
                    "stop:" + _SEND_MODE_SLOW,
                    "tap",
                    "backend-restart",
                )
            )
            ledger.log(f"  {op}")
            if op == "send":
                world.send_now(world.new_text())
            elif op.startswith("stop:"):
                _run_claude_stop(world, op.split(":", 1)[1])
            elif op == "tap":
                _run_claude_tap(world)
            elif op == "backend-restart":
                world.swap_backend_watcher()
            else:
                pytest.fail(f"unknown claude op {op}")
        world.assert_mirror_matches("round settle")
        world.swap_backend_watcher()
        world.settle_turn_end()
        ledger.verify()
    # The on-disk cross-check: user turns in the real session records are exactly the delivered
    # set, once each -- and never a returned or killed message (U1 read straight off disk).
    events = world.watcher.get_all_events()
    on_disk_user_turns = Counter(
        event["content"] for event in events if event.get("type") == "user_message" and event.get("display") is None
    )
    assert on_disk_user_turns == Counter(ledger.delivered), (
        f"on-disk user turns {on_disk_user_turns} != delivered ledger {Counter(ledger.delivered)}"
        f"\n{ledger.replay_note()}"
    )
    for text in ledger.returned + ledger.killed:
        assert on_disk_user_turns.get(text, 0) == 0, (
            f"{text!r} was returned/killed yet appears as a delivered user turn\n{ledger.replay_note()}"
        )


# =============================================================================
# antigravity: the stop + tap executors over the REAL held-queue tracker
# =============================================================================
#
# agy is the only harness whose queue is OURS: it parks mid-turn input invisibly inside its
# TUI, so we never let it park anything and hold the messages backend-side instead (contract
# Part C). That inverts what the storm has to simulate. The other storms replay the real bytes
# an executor wrote, because their harness consumes a file. agy's executor writes tmux
# keystrokes, which leave no bytes -- so the scripted world here plays agy itself: it owns the
# ``active`` marker, and "commits" a delivered block exactly as agy would, merging the whole
# block into ONE turn.


class _AgyWorld:
    """The scripted agy: its busy marker, its transcript, and the turns it commits.

    Two things this world must model honestly, because getting either wrong validates the
    wrong gate with a green test:

    * **The marker can vanish mid-turn**, and does during a backgrounded tool call -- measured
      on agy 1.1.20, 33.5 seconds of it, while the turn's answer had not arrived. So the marker
      is modelled as evidence of busy only, never as proof of idle. (``statusline.sh``'s header
      records the opposite for a SUBAGENT run on 1.0.6/1.0.7; both are true, the cases differ,
      and a design that survives the harsher one survives both.)
    * **Typing into an open turn PARKS and MERGES.** agy does not reject it and does not give
      it its own turn -- the text is absorbed into the running turn, invisibly. So a mid-turn
      delivery is scored as a LOSS here, which is the only way a conservation ledger can see
      the failure this whole design exists to prevent.
    """

    def __init__(self, agent_state_dir: Path, ledger: _Ledger) -> None:
        self.dir = agent_state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger
        self.queue = AntigravityQueueTracker.build(self.dir / "agy_outbox.jsonl", "storm-session")
        self._counter = 0
        self._is_turn_open = False
        self._turn_started_at = time.time() - 1.0
        self._committed_user_turns: list[str] = []
        drop_turn_state(self.dir.name)
        self.turn_state = get_turn_state(self.dir.name)

    # --- the simulated agy ---------------------------------------------------------------

    @property
    def marker(self) -> Path:
        return self.dir / ACTIVE_MARKER_FILENAME

    def begin_turn(self) -> None:
        self._is_turn_open = True
        # The tail row is stamped when the turn's work happened, NOT when we later look at it.
        # A cancelled tool row predates the ctrl+c that abandoned it, which is exactly what
        # lets our cancel stamp release the hold.
        self._turn_started_at = time.time() - 1.0
        self.marker.write_text("")
        self._republish()

    def end_turn(self) -> None:
        self._is_turn_open = False
        self.marker.unlink(missing_ok=True)
        self._republish()

    def run_tool(self) -> None:
        """A BACKGROUNDED tool call mid-turn: the marker VANISHES while the turn stays open.

        Measured on agy 1.1.20 by sampling statusLine directly -- agy reports ``idle`` and stops
        sampling for the duration (33.5s of silence observed), because it genuinely has nothing
        to do while the command runs. The turn is not over: its answer has not arrived. This is
        the shape that punishes anything treating the marker's absence as "no turn is open", so
        it is the shape the storm models.
        """
        if not self._is_turn_open:
            return
        self.marker.unlink(missing_ok=True)
        self._republish()

    def cancel_turn(self) -> None:
        """A ctrl+c. The turn ends, but the transcript tail keeps reading 'open' forever --
        measured on agy 1.1.20, a cancelled tool call settles as CANCELED and the parser
        emits a tool_result for it. Only our own cancel stamp releases it."""
        self._is_turn_open = False
        self.marker.unlink(missing_ok=True)
        self._republish(force_open_tail=True)

    def _republish(self, *, force_open_tail: bool = False) -> None:
        """Publish a transcript whose tail matches the world's state."""
        events: list[dict[str, Any]] = []
        for text in self._committed_user_turns:
            events.append({"type": "user_message", "content": text, "timestamp": _iso_timestamp(time.time())})
        if self._is_turn_open or force_open_tail:
            events.append({"type": "tool_result", "timestamp": _iso_timestamp(self._turn_started_at)})
        else:
            events.append({"type": "assistant_message", "text": "done", "timestamp": _iso_timestamp(time.time())})
        self.turn_state.publish(events, None)

    def is_turn_open(self) -> bool:
        return self._is_turn_open

    def deliver(self, block: str) -> bool:
        """agy receives a typed block. THIS is where the swallow is scored.

        If a turn is open the text is parked and merged: it is absorbed, never answered
        separately, and nothing on disk distinguishes it. mngr still reports success, because
        its probe is the busy marker's mtime -- which is exactly why the ack cannot be trusted.
        """
        texts = _block_texts(block)
        if self._is_turn_open:
            for text in texts:
                self.ledger.killed.append(text)
            return True
        self.begin_turn()
        for text in texts:
            self.ledger.delivered.append(text)
        # ONE turn for the whole block -- measured on agy 1.1.20: an embedded newline is
        # inserted in the composer, not submitted, so one Enter commits the block as a single
        # USER_INPUT row. Crediting each line as its own turn would make the delivery verdict
        # unmatchable and the block would be retyped forever.
        self._committed_user_turns.append(block)
        self._republish()
        return True

    # --- the ops -------------------------------------------------------------------------

    def new_text(self) -> str:
        self._counter += 1
        return f"agy-msg-{self._counter}"

    def send(self, text: str) -> None:
        """The REAL rule: the session never types, it only enqueues."""
        self.ledger.accepted.append(text)
        self.queue.enqueue(text, _iso_timestamp(time.time()))

    def foreign_turn(self) -> None:
        """Someone we do not control -- a human at the tmux pane, or cron -- opens a turn.

        It holds no lock of ours and appears in the same transcript, which is why a delivery
        verdict of "did a turn open?" cannot be trusted and the block's own text is matched.
        """
        if self._is_turn_open:
            return
        self.begin_turn()
        self._committed_user_turns.append("foreign-" + str(self._counter))
        self._republish()


class _AgyStormWatcher(_StormWatcherBase):
    """Exposes the REAL tracker and the REAL turn-open predicate to the executors."""

    def __init__(self, world: _AgyWorld) -> None:
        self._world = world

    def get_queued_messages(self) -> list[dict[str, Any]]:
        return self._world.queue.snapshot()

    def get_queued_block(self) -> str:
        return self._world.queue.concatenated_block()

    def clear_queue(self) -> None:
        self._world.queue.clear()

    def take_unclaimed_queue(self) -> tuple[str, tuple[str, ...]]:
        return self._world.queue.take_unclaimed()

    def take_whole_queue(self) -> tuple[str, tuple[str, ...]]:
        return self._world.queue.take_all()

    def claim_queue_for_tap(self) -> tuple[str, tuple[str, ...], int]:
        return self._world.queue.begin_flush()

    def release_tap_claim(self, claimed: tuple[str, ...], generation: int) -> None:
        self._world.queue.release_claim(claimed, generation)

    def notify_idle(self) -> list[dict[str, Any]]:
        return self._world.queue.snapshot()

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        return None

    def is_main_session_event(self, event: dict[str, Any]) -> bool:
        return True


def _run_agy_worker(world: _AgyWorld) -> None:
    """One tick of the REAL flush worker's decision, against the scripted agy.

    This mirrors ``AntigravitySessionWatcher._attempt_flush`` exactly: liveness, then the
    bounded turn-open gate, then the claim, then the send, then a verdict taken from agy's
    own committed user turns rather than from the send's return value.
    """
    if not world.queue.has_entries():
        return
    if world.turn_state.is_hold_required(world.dir):
        return
    block, claimed, generation = world.queue.begin_flush()
    if not claimed:
        return
    before = world.turn_state.user_turn_texts()
    world.deliver(block)
    after = world.turn_state.user_turn_texts()[len(before) :]
    delivered = claimed if any(text.strip() == block.strip() for text in after) else ()
    world.queue.finish_flush(claimed, generation, delivered=delivered)


def _agy_agent_info(agent_state_dir: Path) -> AgentInfo:
    return AgentInfo(
        id="agy-storm-agent",
        name="agy-storm-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=agent_state_dir / "unused",
        harness=HarnessType.ANTIGRAVITY,
    )


def _run_agy_stop(world: _AgyWorld, watcher: _AgyStormWatcher, agent_info: AgentInfo, send_mode: str) -> None:
    """Drive the REAL stop executor and account for every message it returns.

    ``send_mode`` stages what is in flight when stop lands. With ONE typist an in-flight send
    is by definition a CLAIMED flush, so that is what is staged -- and it is SETTLED after stop
    returns, because the real worker always settles in a ``finally``. A claim that is never
    settled is not a state production can reach.
    """
    in_flight_text = ""
    claimed: tuple[str, ...] = ()
    generation = 0
    if send_mode != _SEND_MODE_NONE:
        in_flight_text = world.new_text()
        world.ledger.accepted.append(in_flight_text)
        world.queue.enqueue(in_flight_text, _iso_timestamp(time.time()))
        _block, claimed, generation = world.queue.begin_flush()

    def restart_process() -> tuple[bool, str]:
        world.end_turn()
        return (True, "ok")

    def press_chord() -> bool:
        # A single ctrl+c ends agy's turn. Its statusline drops the marker, but the transcript
        # tail keeps reading "open" -- which is why only our own cancel stamp releases it.
        world.cancel_turn()
        return True

    executor = AntigravityInterruptToComposer.build(agent_info)
    block = executor.drain_to_composer(watcher, restart_process, lambda: None, press_chord, lambda: "")
    for text in _block_texts(block):
        world.ledger.returned.append(text)

    if claimed:
        # The staged flush now finishes, and it did NOT land: stop cancelled the turn it was
        # sending into. Settling as undelivered returns the entry to the queue, where the
        # ordinary worker delivers it later -- so the world's own ``deliver`` remains the only
        # thing that ever credits a delivery. The settle carries the generation it claimed
        # under, so a stop that voided the claim is respected and this becomes a no-op.
        world.queue.finish_flush(claimed, generation, delivered=())
    world.end_turn()


def _run_agy_tap(world: _AgyWorld, watcher: _AgyStormWatcher, agent_info: AgentInfo) -> None:
    """Drive the REAL tap executor.

    It must NOT deliver: a tap that sent could race the flush worker for the same block and
    deliver it twice. It cancels, releases its claim, and the one typist delivers.
    """
    if not world.is_turn_open():
        world.begin_turn()

    def _must_not_send(_block: str) -> bool:
        raise AssertionError("the tap must never send: the flush worker is the only typist")

    executor = AntigravityAtomicShoulderTap.build(agent_info)
    executor.tap(watcher, lambda: (world.cancel_turn(), True)[1], _must_not_send)
    # The tap wakes the worker rather than delivering; run it, as the real loop would.
    _run_agy_worker(world)


def test_antigravity_conservation_storm_stop_and_tap_executors(tmp_path: Path) -> None:
    """Seeded rounds of interleaved agy sends / stops / taps under real lock contention.

    The property is the same as the other storms (contract A1): every accepted message ends in
    exactly one terminal state. agy's specific risk is its MERGING -- one committed turn
    discharges the whole held block -- so a bug here shows up as duplicates (a block counted
    twice) or losses (a block cleared without being committed), which the ledger catches.
    """
    ledger = _Ledger("antigravity")
    world = _AgyWorld(tmp_path / "agy", ledger)
    watcher = _AgyStormWatcher(world)
    agent_info = _agy_agent_info(world.dir)
    for round_index in range(_ROUND_COUNT):
        rng = random.Random(_BASE_SEED + round_index)
        ledger.log(f"round {round_index}:")
        for op_index in range(rng.randint(2, 4)):
            if round_index in _STOP_HAMMER_ROUNDS and op_index == 0:
                op = "stop:" + _SEND_MODE_SLOW
            else:
                op = rng.choice(
                    (
                        "send",
                        "send",
                        "busy_send",
                        "tool_call",
                        "foreign_turn",
                        "worker",
                        "worker",
                        "tap",
                        "flush",
                        "stop:" + _SEND_MODE_NONE,
                        "stop:" + _SEND_MODE_FAST,
                    )
                )
            ledger.log(f"  {op}")
            if op == "tool_call":
                # A tool call mid-turn. The marker STAYS (statusline.sh's measured behaviour);
                # what makes this dangerous is simply that the turn is still open.
                world.run_tool()
            elif op == "foreign_turn":
                # A turn WE did not open -- a human at the tmux pane, or cron. It holds no lock
                # of ours and lands in the same transcript, which is why a delivery verdict of
                # "did a turn open?" is not good enough.
                world.foreign_turn()
            elif op == "worker":
                _run_agy_worker(world)
            elif op == "send":
                world.send(world.new_text())
            elif op == "busy_send":
                # The case the whole design exists for: a send while a turn is open.
                world.begin_turn()
                world.send(world.new_text())
            elif op == "tap":
                _run_agy_tap(world, watcher, agent_info)
            elif op == "flush":
                _run_agy_worker(world)
            elif op.startswith("stop:"):
                _run_agy_stop(world, watcher, agent_info, op.split(":", 1)[1])
            else:
                pytest.fail(f"unknown agy op {op}")
        # Settle: agy finishes its turn, then the worker drains whatever is still held.
        # PROGRESS, not just conservation -- a queue held forever is in exactly one state and
        # still never arrives, so the storm asserts it actually empties.
        for _ in range(_SETTLE_TICKS):
            # agy finishes whatever turn is running, then the worker gets one chance. Each
            # delivery OPENS a turn (that is what a delivery is), so the turn must close
            # between ticks -- exactly what the real worker waits for.
            world.end_turn()
            if not world.queue.has_entries():
                break
            _run_agy_worker(world)
        world.end_turn()
        assert not world.queue.has_entries(), (
            "the queue must drain once the turn is closed -- a permanently held queue is a "
            f"strand, not conservation.\n{ledger.replay_note()}"
        )
        ledger.verify()


@pytest.mark.parametrize("is_flush_slow", (False, True))
def test_antigravity_interrupt_during_a_flush_conserves_every_message(tmp_path: Path, is_flush_slow: bool) -> None:
    """Plan section 8's required case: stop lands while a flush has the queue CLAIMED and in flight.

    The storm's ops run one after another on a single thread, so it never stages this: the
    flush hands its block to mngr's send, which holds ``message.lock`` for the whole
    submission, and stop arrives during exactly that window. Both interleavings must conserve.

    Stop deliberately does NOT take the claimed entries: that send may still land, and handing
    them to the composer as well is how one message becomes both Delivered and Returned. It
    also does not void the claim, so the flush can still settle -- voiding it would make the
    settle stale, return the entries to the queue, and deliver a block agy had already
    committed a second time.

    - fast flush: it settles before stop finishes, so the entries are delivered and gone.
    - slow flush: it settles after, under the generation it claimed with, and is honoured.

    Either way every message ends in exactly one state and the queue drains.
    """
    ledger = _Ledger(f"antigravity-interrupt-during-flush[slow={is_flush_slow}]")
    world = _AgyWorld(tmp_path / "agy-flush", ledger)
    watcher = _AgyStormWatcher(world)
    agent_info = _agy_agent_info(world.dir)

    # Two messages held behind an open turn, then claimed by a flush.
    world.begin_turn()
    for _ in range(2):
        world.send(world.new_text())
    block, claimed, generation = world.queue.begin_flush()
    assert claimed, "the flush must have something to claim"

    restart_count = {"value": 0}

    def deliver() -> None:
        """mngr's send, completing while it still holds the lock (as _InFlightSend documents)."""
        if restart_count["value"]:
            # The hammer's restart landed mid-send: nothing committed. The entries were never
            # removed from our queue on claim, so stop's block already handed them back -- they
            # are Returned, NOT killed. This is where agy conserves better than claude's E2
            # corner, whose in-flight text lives inside the harness and dies with it.
            world.queue.finish_flush(claimed, generation, delivered=())
            return
        world.deliver(block)
        world.queue.finish_flush(claimed, generation, delivered=claimed)

    hold = _SLOW_HOLD_SECONDS if is_flush_slow else _FAST_HOLD_SECONDS
    in_flight = _InFlightSend(world.dir, hold, deliver)

    def restart_process() -> tuple[bool, str]:
        restart_count["value"] += 1
        world.end_turn()
        return (True, "ok")

    def press_chord() -> bool:
        world.cancel_turn()
        return True

    executor = AntigravityInterruptToComposer.build(agent_info)
    returned = executor.drain_to_composer(watcher, restart_process, lambda: None, press_chord, lambda: "")
    for text in _block_texts(returned):
        ledger.returned.append(text)
    in_flight.join()

    world.end_turn()
    _run_agy_worker(world)
    ledger.verify()
    assert not world.queue.has_entries(), "the queue must drain rather than strand"
    assert len(ledger.delivered) == 2, "both messages must reach agy exactly once"
    assert ledger.returned == [], "stop must not hand back a block whose flush it left running"


def test_antigravity_tap_delivers_once_and_returns_nothing(tmp_path: Path) -> None:
    """A tap must not hand its queue back to the composer.

    ``ShoulderTapOutcome.block`` is a returned-to-composer handback for a tap that FAILED.
    Returning the queue there on success made the frontend prepend every held message to the
    composer while the worker was also delivering it -- sent AND drained back, seen live.
    """
    ledger = _Ledger("antigravity-tap-handback")
    world = _AgyWorld(tmp_path / "agy-tap", ledger)
    watcher = _AgyStormWatcher(world)
    agent_info = _agy_agent_info(world.dir)

    world.begin_turn()
    for _ in range(2):
        world.send(world.new_text())

    def _must_not_send(_block: str) -> bool:
        raise AssertionError("the tap must never send: the flush worker is the only typist")

    outcome = AntigravityAtomicShoulderTap.build(agent_info).tap(
        watcher, lambda: (world.cancel_turn(), True)[1], _must_not_send
    )
    assert outcome.status == "flushed"
    assert outcome.block == "", "nothing goes back to the composer on a successful tap"
    assert len(world.queue.snapshot()) == 2, "the queue is still the worker's to deliver"

    _run_agy_worker(world)
    assert len(ledger.delivered) == 2
    ledger.verify()
    assert not world.queue.has_entries()
