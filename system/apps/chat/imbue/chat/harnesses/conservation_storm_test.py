"""Seeded message-conservation storms for the claude, pi, antigravity and grok stop/flush executors (invariant U1).

THE PROPERTY (system/apps/chat/imbue/chat/harnesses/core-contracts/messages-lifecycle-contract.md A1, U1): across any
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

antigravity and grok hold their queue in the chat, and one flush worker types it, so their scripted
worlds play the harness itself. grok's world also plays mngr: its root-turn files, which outlive
grok's own turn end, its REFUSE busy-send policy and its Ctrl+C rule, reading the world's pane with
mngr_grok's own predicates. grok's storm drives the real tracker, flush worker, session send, stop,
tap and model-bar control lane, and checks after every op that each accepted message is in exactly
one state.

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
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import pytest

from imbue.chat.activity_state import ACTIVE_MARKER_FILENAME
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import SendFailedError
from imbue.chat.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.chat.harnesses.antigravity.tap import AntigravityAtomicShoulderTap
from imbue.chat.harnesses.antigravity.tap import AntigravityInterruptToComposer
from imbue.chat.harnesses.antigravity.turn_state import drop_turn_state
from imbue.chat.harnesses.antigravity.turn_state import get_turn_state
from imbue.chat.harnesses.claude.session_parser import INTERRUPT_SENTINEL_TEXT
from imbue.chat.harnesses.claude.tap import ClaudeTapStatus
from imbue.chat.harnesses.claude.tap import execute_claude_shoulder_tap
from imbue.chat.harnesses.claude.tap import execute_claude_stop_to_composer
from imbue.chat.harnesses.claude.watcher import ClaudeSessionWatcher
from imbue.chat.harnesses.grok.model import GrokModelResolver
from imbue.chat.harnesses.grok.queue_tracker import drop_tracker as drop_grok_tracker
from imbue.chat.harnesses.grok.queue_tracker import get_tracker as get_grok_tracker
from imbue.chat.harnesses.grok.session import GrokHarnessSession
from imbue.chat.harnesses.grok.tap import GrokAtomicShoulderTap
from imbue.chat.harnesses.grok.tap import GrokInterruptToComposer
from imbue.chat.harnesses.grok.testing import GROK_PID_RELATIVE_PATH
from imbue.chat.harnesses.grok.testing import add_grok_session
from imbue.chat.harnesses.grok.testing import append_grok_updates
from imbue.chat.harnesses.grok.testing import grok_update_line
from imbue.chat.harnesses.grok.testing import make_grok_agent_info
from imbue.chat.harnesses.grok.testing import write_live_grok_pid
from imbue.chat.harnesses.grok.testing import write_root_turn
from imbue.chat.harnesses.grok.watcher import GrokSessionWatcher
from imbue.chat.harnesses.grok.watcher import GrokTranscriptLoader
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.interrupt import InterruptToComposer
from imbue.chat.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.chat.harnesses.interrupt import restart_drain
from imbue.chat.harnesses.interrupt import try_hold_message_lock
from imbue.chat.harnesses.model import ModelAxis
from imbue.chat.harnesses.model import ModelIdentity
from imbue.chat.harnesses.pi_coding.inbox import PI_INBOX_NAME
from imbue.chat.harnesses.pi_coding.inbox import PI_INTERRUPT_KEY
from imbue.chat.harnesses.pi_coding.inbox import PI_RETRACT_KEY
from imbue.chat.harnesses.pi_coding.model import PiFlushTapStatus
from imbue.chat.harnesses.pi_coding.model import PiInterruptToComposer
from imbue.chat.harnesses.pi_coding.model import flush_pi_queue_atomic
from imbue.chat.harnesses.session import AtomicShoulderTap
from imbue.chat.harnesses.session import SendOutcome
from imbue.chat.harnesses.session import SessionDeps
from imbue.chat.harnesses.session_watcher import AgentSessionWatcher
from imbue.chat.harnesses.session_watcher import OnEventsCallback
from imbue.chat.testing import agent_message_lock
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_grok.api import PROCESS_STARTED_MARKER_FILENAME
from imbue.mngr_grok.api import composer_holds_text
from imbue.mngr_grok.api import detect_gate_screen
from imbue.mngr_grok.api import get_model_state_path as get_grok_model_state_path
from imbue.mngr_grok.api import get_root_turns
from imbue.mngr_grok.api import get_root_turns_dir
from imbue.mngr_grok.api import has_cancel_hint
from imbue.mngr_grok.api import is_root_turn_in_flight

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
    block = interrupter.drain_to_composer(
        watcher, world.restart_process, lambda: None, lambda: True, lambda: "", lambda: False
    )
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


@pytest.mark.flaky
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
        # 2s, not 1s: the release condition subtracts a 1.0s timestamp-granularity
        # allowance (turn_state._TIMESTAMP_GRANULARITY_SECONDS) from the cancel stamp, so a
        # tail exactly 1.0s old leaves a sub-millisecond margin decided by the ISO
        # timestamp's millisecond rounding -- a real intermittent failure on fast runs. A 2s
        # tail keeps the margin at a full second while staying "recent" for the open-tail
        # rung.
        self._turn_started_at = time.time() - 2.0
        self._committed_user_turns: list[str] = []
        drop_turn_state(self.dir.name)
        self.turn_state = get_turn_state(self.dir.name)

    @property
    def marker(self) -> Path:
        return self.dir / ACTIVE_MARKER_FILENAME

    def begin_turn(self) -> None:
        self._is_turn_open = True
        # The tail row is stamped when the turn's work happened, NOT when we later look at it.
        # A cancelled tool row predates the ctrl+c that abandoned it, which is exactly what
        # lets our cancel stamp release the hold.
        self._turn_started_at = time.time() - 2.0
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
    block = executor.drain_to_composer(watcher, restart_process, lambda: None, press_chord, lambda: "", lambda: False)
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
    returned = executor.drain_to_composer(
        watcher, restart_process, lambda: None, press_chord, lambda: "", lambda: False
    )
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


# grok keeps the queue it shows the user inside its TUI too, so the chat holds the messages and one flush worker types
# them, as for agy. What tells that worker grok is busy is different: mngr's root-turn files, which outlive grok's own
# turn end, and mngr's REFUSE busy-send policy, under which mngr types nothing into a running turn. So the scripted
# world plays grok AND mngr (its hooks, its send and its Ctrl+C rule), and everything on the chat's side of mngr is the
# real code: the tracker, the flush worker, the session's send, stop, the tap and the model bar's control lane.

_GROK_PENDING_WAKE_PREFIX = "pending-wake:"
_GROK_CONTROL_VERBS = frozenset({"/model", "/effort"})

# How long a root-turn file outlives its turn once something waits on it in real time: mngr's StopCancelled hook clears
# a cancelled turn's 39-65 ms after the cancel, and its streamer a finished turn's within its next poll.
_GROK_TURN_FILE_LAG_SECONDS = 0.03
# How long stop and the tap wait for a turn to end: one their press ends, and one it cannot (which ends the same way
# however long they wait).
_GROK_TURN_END_DEADLINE_SECONDS = 2.0
_GROK_STRANDED_TURN_DEADLINE_SECONDS = 0.0
# How long a POST waits on the flush worker's first send of its message; a send that settles answers it at once.
_GROK_SEND_RESULT_WAIT_SECONDS = 3.0
# How long a test waits on a staged step that happens at once.
_GROK_STEP_WAIT_SECONDS = 5.0
_GROK_THREAD_JOIN_SECONDS = 30.0
_GROK_MAX_WAKES_PER_OP = 8

# Logical seconds per storm op, against grok's quit window and mngr's spacing between two Ctrl+C presses.
_GROK_OP_SECONDS = 0.4
_GROK_QUIT_WINDOW_SECONDS = 1.0
_MNGR_CTRL_C_SPACING_SECONDS = 1.5

_GROK_RECORD_CLOCK_BASE_MS = 1_792_500_000_000
_GROK_PROCESS_CLOCK_BASE_NS = 1_792_500_000_000_000_000
_GROK_SETTLE_TICKS = 16
# Rounds that open with a scripted run of ops, for paths random ops reach too rarely to rely on.
_GROK_STAGED_ROUNDS: dict[int, tuple[str, ...]] = {
    # A placeholder holds a message, and stop and the tap find no turn to cancel.
    3: ("spawn_subagent", "turn_ends", "streamer", "subagent_ends", "send", "tap", "stop", "wake_starts", "send"),
    # A turn whose composer holds a draft, which mngr will not press into: stop restarts grok.
    7: ("send", "stop:stranded"),
    # A gate with a model switch held in the control lane, and a message sent to it.
    11: ("turn_ends", "streamer", "gate", "model_switch", "send", "gate"),
    # The restart meets the consent notice: everything comes back, and the next message fails at the gate.
    19: ("send", "stop:restart_fails", "send", "gate"),
    # grok exits with messages held: its relaunch, started by the next send, drops them with the old session.
    23: ("send", "send", "grok_exits", "send"),
    # grok runs a slash command itself with no turn for it; mngr types the next message after a draft left at the pane.
    15: ("settle", "slash", "idle_draft", "send", "slash", "send", "turn_ends"),
    # A wake turn starts inside mngr's send and outlives the witness window, so grok runs the paste after it.
    25: ("settle", "race:paste_long", "send", "stop"),
    # grok exits with nothing held, so nothing wakes the worker at its relaunch before the reviving send lands.
    27: ("turn_ends", "streamer", "grok_exits", "send"),
}
_GROK_MODEL_PICKS = (("grok-4.6", "xhigh"), ("grok-4.6", "low"), ("grok-4.5", "medium"), ("grok-4.5", "high"))

# grok 1.0.62's pane as mngr_grok's captures show it: the composer box, the footer under it, and the consent gate.
_GROK_BOX_TOP = "  ╭" + "─" * 74 + "╮"
_GROK_BOX_BOTTOM = "  ╰" + "─" * 67 + " Stub ─╯"
_GROK_IDLE_FOOTER = "  Shift+Tab:mode  │  Ctrl+x:shortcuts"
_GROK_IDLE_TYPED_FOOTER = "  Enter:send  │  Opt+Enter:newline  │  Shift+Tab:mode  │  Ctrl+x:shortcuts"
_GROK_BUSY_FOOTER = "  Shift+Tab:mode  │  Ctrl+c:cancel  │  Ctrl+x:shortcuts"
_GROK_BUSY_TYPED_FOOTER = "  Enter:queue  │  Opt+Enter:newline  │  Shift+Tab:mode  │  Ctrl+c:cancel  │"
_GROK_BUSY_QUEUED_FOOTER = (
    "  Enter:send now  │  Shift+Tab:mode  │  Ctrl+c:cancel  │  Ctrl+;:queue  │  Ctrl+x:shortcuts"
)
_GROK_PRESS_AGAIN_FOOTER = "  Ctrl+c:press again to quit"
_GROK_CONSENT_PANE = "\n".join(
    (
        "",
        "  Updates to our terms",
        "  Got it                       a",
        "  Quit                         q",
        "",
        "Grok Build  1.0.62",
        "",
    )
)

# Every op the storm draws from, weighted by repetition.
_GROK_STORM_OPS = (
    "send",
    "send",
    "send",
    "send",
    "send",
    "send",
    "race:worker",
    "race:preflight",
    "race:paste",
    "worker",
    "turn_ends",
    "turn_ends",
    "streamer",
    "streamer",
    "spawn_subagent",
    "subagent_ends",
    "subagent_ends",
    "wake_starts",
    "spawn_shell",
    "shell_ends",
    "pane_turn",
    "pane_draft",
    "gate",
    "gate",
    "grok_exits",
    "model_switch",
    "model_switch",
    "emit",
    "tap",
    "tap",
    "tap",
    "stop",
    "stop",
    "stop",
)
# The paths each grok storm must reach, so a reseed that stops exercising one fails loudly.
_GROK_REQUIRED_PATHS = (
    "cancel",
    "press_refused",
    "placeholder",
    "wake",
    "race:worker",
    "race:preflight",
    "race:paste",
    "send_refused_busy",
    "send_blocked",
    "post_failed",
    "restart",
    "restart_failed",
    "control_applied",
    "gate",
    "exit",
    "local_command",
    "draft_appended",
    "parked_long",
)


def _do_nothing() -> None:
    pass


def _is_never_true() -> bool:
    return False


def _is_always_true() -> bool:
    return True


def _is_grok_control_command(text: str) -> bool:
    words = text.split(maxsplit=1)
    return bool(words) and words[0] in _GROK_CONTROL_VERBS


def _grok_flush_hook_must_not_send(_text: str) -> bool:
    raise AssertionError("the session's bound send carries mngr's refusal kind, so the flush hook is never used")


def _grok_tap_must_not_send(_text: str) -> bool:
    raise AssertionError("the tap must never send: the flush worker is grok's only typist")


def _grok_bar_must_not_type(_text: str) -> bool:
    raise AssertionError("the model bar must not type: its commands go through the flush worker's control lane")


class _GrokWorld:
    """The scripted grok behind mngr: its turns, its own queue and pane, mngr's hooks, and mngr's send and Ctrl+C rules.

    What it models, as measured on grok 1.0.62 under mngr, because modelling any of it wrong validates the wrong gate:

    * **mngr's root-turn file outlives grok's turn.** grok logs ``turn_completed`` and goes idle at the pane; mngr's
      streamer clears the file at its next poll (the ``streamer`` op), and its StopCancelled hook 39-65 ms after a
      cancel. In between mngr still reports the turn in flight.
    * **Background work.** A background subagent keeps ``active`` with no root turn. One that ends while grok is idle
      leaves a ``pending-wake:`` placeholder until grok starts its wake turn; a background shell's wake turn starts at
      once with no placeholder. Work that ends during a turn wakes grok when that turn ends.
    * **The pane**, rendered from grok's own footers and read with mngr_grok's own predicates: the busy footer's
      ``Ctrl+c:cancel``, the composer's text, grok's ``#N`` queue rows, a pending quit and a consent gate.
    * **Ctrl+C.** It clears composer text and the turn runs on; it cancels a busy turn with an empty composer, after
      which grok runs whatever it holds; on an idle grok it arms quit, and a second press inside a second QUITS grok,
      which is fatal here.
    * **mngr's send under REFUSE** types nothing into a running turn or a placeholder (AGENT_BUSY) or a gate
      (INPUT_BLOCKED). A turn that starts between that check and the paste still parks the paste in grok's own queue,
      where it runs as its own turn once that turn ends, which can be well after the worker's witness window.
    * **Slash commands and drafts.** grok runs a slash command itself and logs no turn for it. mngr types a message
      after a draft left in grok's idle composer, grok logs both as one turn, and mngr, looking for the message's
      own prompt, cannot confirm it. A `/model` or `/effort` rewrites mngr_grok's model_state.json.
    * **Exits and launches.** grok can exit (an OOM shed or a crash), and the chat's next send starts it again. Every
      launch touches the process marker, so the chat's queue, bound to the old session, dies with it (contract Part
      B); a restart that meets the consent notice leaves grok up at the gate with no session.
    """

    def __init__(self, agent_state_dir: Path, ledger: _Ledger) -> None:
        self.dir = agent_state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ledger = ledger
        self.clock = 0.0
        self.session_id = ""
        self.turn_prompt_id: str | None = None
        self.grok_queue: list[str] = []
        self.composer = ""
        self.is_gated = False
        self.quit_armed_at: float | None = None
        self.subagent_ids: list[str] = []
        self.shell_ids: list[str] = []
        # Background work that ended during a turn, whose wake turn grok starts when the turn ends.
        self.due_wakes: list[tuple[str, str]] = []
        # The ended turn whose root-turn file mngr has not cleared yet.
        self.lagging_prompt_id: str | None = None
        # The ended subagent whose wake turn mngr's placeholder stands in for.
        self.placeholder_child_id: str | None = None
        self.last_press_at: float | None = None
        self.requested_by_verb: dict[str, str] = {}
        self.applied_by_verb: dict[str, str] = {}
        self.restart_count = 0
        # The slash commands grok ran itself, logging no turn, and each turn mngr typed after a person's draft.
        self.local_commands_run: list[str] = []
        self.delivered_text_by_drafted_turn: dict[str, str] = {}
        self.is_last_paste_after_draft = False
        # The prompt of every top-level turn grok started, and the turn running when grok was last restarted.
        self.prompt_text_by_id: dict[str, str] = {}
        self.prompt_id_at_last_restart: str | None = None
        self.stats: Counter[str] = Counter()
        self.is_running = False
        self.is_worker_race_armed = False
        self.is_preflight_race_armed = False
        self.is_paste_race_armed = False
        # Whether a raced wake turn outlives the flush worker's witness window.
        self.is_raced_wake_long = False
        self.is_next_restart_failing = False
        # A test stages a slow send with these: the paste waits for the gate, and the confirmation for stop's press.
        self.paste_gate: threading.Event | None = None
        self.is_confirm_held_for_press = False
        self.send_holds_lock = threading.Event()
        self.press_waits_for_lock = threading.Event()
        self.on_turn_files_changed: Callable[[], None] = _do_nothing
        self._lock = threading.RLock()
        self._timers: list[threading.Timer] = []
        self._is_turn_files_changed = False
        self._process_count = 0
        self._prompt_index = 0
        self._record_ms = _GROK_RECORD_CLOCK_BASE_MS
        self._message_count = 0
        self._id_count = 0
        self._launch(is_gated=False)

    # identity and reads

    def new_text(self) -> str:
        self._message_count += 1
        return f"grok-msg-{self._message_count:03d}"

    def new_command(self) -> str:
        """A slash command grok runs itself, such as `/new` or `/timestamps`."""
        self._message_count += 1
        return f"/storm-command-{self._message_count:03d}"

    def _next_id(self, prefix: str) -> str:
        self._id_count += 1
        return f"{prefix}-{self._id_count:03d}"

    def is_alive(self) -> bool:
        return self.is_running

    def advance(self) -> None:
        with self._lock:
            self.clock += _GROK_OP_SECONDS

    def cancellable_prompt_id(self) -> str | None:
        """The top-level turn grok is running, which a Ctrl+C would cancel, if any."""
        with self._lock:
            return self.turn_prompt_id if self.is_running and not self.is_gated else None

    def is_turn_stranded(self) -> bool:
        """Whether a running turn outlives a stop: its Ctrl+C is refused for composer text, or grok runs more after it."""
        with self._lock:
            return self.turn_prompt_id is not None and bool(self.composer or self.grok_queue or self.due_wakes)

    def is_quiet(self) -> bool:
        with self._lock:
            return (
                self.is_running
                and self.turn_prompt_id is None
                and self.lagging_prompt_id is None
                and self.placeholder_child_id is None
                and not self.is_gated
                and not self.grok_queue
            )

    def render_pane(self) -> str:
        """grok's pane as a plain capture shows it, read back through mngr_grok's predicates before it is returned."""
        if self.is_gated:
            pane = _GROK_CONSENT_PANE
        else:
            rows = [""]
            rows.extend(
                f"    #{index} {text.split(chr(10))[0]}" for index, text in enumerate(self.grok_queue, start=1)
            )
            if self.turn_prompt_id is not None:
                rows.extend(("", "    ⠸ Responding… 2.9s"))
            rows.extend(("", _GROK_BOX_TOP, f"  │ ❯ {self.composer:<72}│", _GROK_BOX_BOTTOM, "", self._footer(), ""))
            pane = "\n".join(rows)
        is_quit_armed = self._is_quit_armed()
        is_busy = self.turn_prompt_id is not None and not self.is_gated
        assert (detect_gate_screen(pane) is not None) == self.is_gated, f"the gate misreads:\n{pane}"
        assert has_cancel_hint(pane) == (is_busy and not is_quit_armed), f"the cancel hint misreads:\n{pane}"
        assert composer_holds_text(pane) == (bool(self.composer) and not is_quit_armed), (
            f"the composer misreads:\n{pane}"
        )
        return pane

    def _is_quit_armed(self) -> bool:
        return self.quit_armed_at is not None and self.clock - self.quit_armed_at < _GROK_QUIT_WINDOW_SECONDS

    def _footer(self) -> str:
        if self._is_quit_armed():
            return _GROK_PRESS_AGAIN_FOOTER
        if self.turn_prompt_id is None:
            return _GROK_IDLE_TYPED_FOOTER if self.composer else _GROK_IDLE_FOOTER
        if self.composer:
            return _GROK_BUSY_TYPED_FOOTER
        return _GROK_BUSY_QUEUED_FOOTER if self.grok_queue else _GROK_BUSY_FOOTER

    # what grok and the people at its pane do

    def end_turn(self) -> None:
        """grok finishes its turn on its own; mngr's file for it stays until the streamer catches up."""
        with self._acting():
            self._end_turn()

    def catch_up_streamer(self) -> None:
        """mngr's streamer reads grok's ``turn_completed``: the finished turn's root-turn file goes."""
        with self._acting():
            if self.lagging_prompt_id is not None:
                self._complete_turn_file(self.lagging_prompt_id)

    def catch_up_streamer_soon(self) -> None:
        """The streamer catches up in real time, while an executor waits on the file."""
        with self._lock:
            lagging_prompt_id = self.lagging_prompt_id
            if lagging_prompt_id is not None:
                self._schedule(lambda: self._complete_turn_file(lagging_prompt_id))

    def spawn_subagent(self) -> None:
        with self._acting():
            if self.turn_prompt_id is not None:
                self.subagent_ids.append(self._next_id("subagent"))
                self._recompute_active()

    def end_subagent(self) -> None:
        """A background subagent ends: grok wakes for it now, after the running turn, or behind mngr's placeholder."""
        with self._acting():
            if not self.subagent_ids:
                return
            child_id = self.subagent_ids.pop(0)
            self._log({"sessionUpdate": "subagent_finished", "subagent_id": child_id, "will_wake": True})
            if self.turn_prompt_id is not None or self.is_gated or self.placeholder_child_id is not None:
                self.due_wakes.append(("subagent", child_id))
            elif self.lagging_prompt_id is not None:
                # grok, already idle, starts the wake before the streamer catches up with the turn it finished.
                self._start_wake_turn("subagent", child_id)
            else:
                self.placeholder_child_id = child_id
                self._write_turn_file(_GROK_PENDING_WAKE_PREFIX + child_id, "<auto-wake>")
                self.stats["placeholder"] += 1
            self._recompute_active()

    def start_pending_wake(self) -> None:
        """grok starts the wake turn mngr's placeholder stood in for."""
        with self._acting():
            if self.placeholder_child_id is not None and self.turn_prompt_id is None and not self.is_gated:
                self._start_wake_turn("subagent", self.placeholder_child_id)

    def spawn_shell(self) -> None:
        with self._acting():
            if self.turn_prompt_id is not None:
                self.shell_ids.append(self._next_id("shell"))

    def end_shell(self) -> None:
        """A background shell ends: an idle grok starts its wake turn at once, with no placeholder before it."""
        with self._acting():
            if not self.shell_ids:
                return
            task_id = self.shell_ids.pop(0)
            if self.turn_prompt_id is not None or self.is_gated or self.placeholder_child_id is not None:
                self._log(
                    {"sessionUpdate": "task_completed", "task_snapshot": {"task_id": task_id}, "will_wake": True}
                )
                self.due_wakes.append(("shell", task_id))
            else:
                self._end_shell_and_wake(task_id)

    def pane_turn(self) -> None:
        """A person at grok's pane sends a message of their own: grok runs it, or queues it behind the running turn."""
        with self._acting():
            if self.is_gated or not self.is_running:
                return
            text = self._next_id("pane-turn")
            if self.turn_prompt_id is None:
                self._start_user_turn(text)
            else:
                self.grok_queue.append(text)

    def pane_draft(self) -> None:
        """A person types into grok's composer during a turn, and sends it once the turn ends."""
        with self._acting():
            if self.turn_prompt_id is not None and not self.composer:
                self.composer = self._next_id("pane-draft")

    def pane_idle_draft(self) -> None:
        """A person types into an idle grok's composer and leaves it there."""
        with self._acting():
            if self.is_running and not self.is_gated and self.turn_prompt_id is None and not self.composer:
                self.composer = self._next_id("pane-draft")

    def strand_turn(self) -> None:
        """A running turn whose composer holds a person's draft, which mngr will not press Ctrl+C into."""
        with self._acting():
            if not self.is_running:
                self._launch(is_gated=False)
            if self.is_gated:
                self._clear_gate()
            if self.turn_prompt_id is None:
                self._start_user_turn(self._next_id("pane-turn"))
            if not self.composer:
                self.composer = self._next_id("pane-draft")

    def toggle_gate(self) -> None:
        """A consent notice comes up on a grok with no turn running, or the person accepts the one that is up."""
        with self._acting():
            if self.is_gated:
                self._clear_gate()
            elif self.is_running and self.turn_prompt_id is None:
                self.is_gated = True
                self.stats["gate"] += 1
            else:
                self.stats["gate_deferred"] += 1

    def arm_worker_race(self) -> None:
        """A background shell from an earlier turn ends right after the chat's next send checks for a turn."""
        with self._lock:
            self.is_worker_race_armed = True

    def start_worker_race(self) -> None:
        """The armed shell's wake turn starts now, between a send's hold check and the flush worker's."""
        with self._acting():
            if self.is_worker_race_armed and self.is_running and not self.is_gated and self.turn_prompt_id is None:
                self.is_worker_race_armed = False
                self.stats["race:worker"] += 1
                self._end_shell_and_wake(self._next_id("shell"))

    def arm_preflight_race(self) -> None:
        """A background shell from an earlier turn ends just before mngr's next send checks grok."""
        with self._lock:
            self.is_preflight_race_armed = True

    def arm_paste_race(self, *, is_wake_long: bool = False) -> None:
        """A background shell from an earlier turn ends between mngr's next check and its paste."""
        with self._lock:
            self.is_paste_race_armed = True
            self.is_raced_wake_long = is_wake_long

    def exit_grok(self) -> None:
        """grok's process exits (an OOM shed or a crash): mngr's SessionEnd hook clears its turn, and what it held dies."""
        with self._acting():
            if not self.is_running:
                return
            self.stats["exit"] += 1
            self.is_running = False
            (self.dir / GROK_PID_RELATIVE_PATH).unlink()
            self._reset_process_state(is_gated=False)
            self._recompute_active()

    def start_grok(self) -> None:
        """``mngr start`` on a grok that exited."""
        with self._acting():
            if not self.is_running:
                self._launch(is_gated=False)

    def settle_step(self) -> None:
        """One step toward a quiet grok: the gate accepted, the turn finished, the streamer caught up, a wake started."""
        with self._acting():
            if not self.is_running:
                self._launch(is_gated=False)
                return
            if self.is_gated:
                self._clear_gate()
                return
            if self.turn_prompt_id is not None:
                self._end_turn()
                return
            if self.lagging_prompt_id is not None:
                self._complete_turn_file(self.lagging_prompt_id)
                return
            if self.placeholder_child_id is not None:
                self._start_wake_turn("subagent", self.placeholder_child_id)

    # mngr

    def press_ctrl_c(self) -> bool:
        """mngr's Ctrl+C into grok (``press_key_chord`` under the message lock), then grok's answer to the key."""
        self.press_waits_for_lock.set()
        with agent_message_lock(self.dir):
            with self._acting():
                if self.last_press_at is not None:
                    self.clock = max(self.clock, self.last_press_at + _MNGR_CTRL_C_SPACING_SECONDS)
                if self._find_press_refusal(self.render_pane()) is not None:
                    self.stats["press_refused"] += 1
                    return False
                self.last_press_at = self.clock
                self._take_ctrl_c()
                return True

    def _find_press_refusal(self, pane: str) -> str | None:
        """mngr's rule: Ctrl+C only into a cancellable top-level turn whose footer shows the cancel hint, composer empty."""
        if detect_gate_screen(pane) is not None:
            return "a gate screen holds the keyboard"
        if not is_root_turn_in_flight(self.dir, is_pending_wake_counted=False):
            return "no cancellable top-level turn is in flight"
        if not has_cancel_hint(pane):
            return "the footer shows no cancel hint"
        if composer_holds_text(pane):
            return "the composer holds text"
        return None

    def mngr_send(self, text: str) -> bool:
        """mngr's send under the REFUSE busy-send policy: the check, then the paste, all under the message lock."""
        assert self.is_running, (
            f"the flush worker sent {text!r} to a grok that is not running\n{self.ledger.replay_note()}"
        )
        with agent_message_lock(self.dir):
            with self._acting():
                self._check_send(text)
            self.send_holds_lock.set()
            if self.paste_gate is not None:
                assert self.paste_gate.wait(timeout=_GROK_THREAD_JOIN_SECONDS), "the staged paste was never released"
            with self._acting():
                self._paste(text)
                is_after_draft = self.is_last_paste_after_draft
            if self.is_confirm_held_for_press:
                assert self.press_waits_for_lock.wait(timeout=_GROK_THREAD_JOIN_SECONDS), "stop never pressed"
        if is_after_draft:
            # Measured live: mngr's evidence looks for the message's own prompt, which the draft ahead of it hides.
            raise SendFailedError("Message submission was not confirmed", SendFailureKind.SUBMISSION_UNCONFIRMED.value)
        return True

    def restart_process(self) -> tuple[bool, str]:
        """``mngr start --restart --no-resume``: grok dies with its turn and its own queue, and relaunches."""
        with self._acting():
            self.restart_count += 1
            self.stats["restart"] += 1
            self.prompt_id_at_last_restart = self.turn_prompt_id
            is_failing = self.is_next_restart_failing
            self.is_next_restart_failing = False
            self._launch(is_gated=is_failing)
            if is_failing:
                self.stats["restart_failed"] += 1
                return (False, "grok shows its consent screen")
            return (True, "ok")

    def join_timers(self) -> None:
        with self._lock:
            timers = list(self._timers)
            self._timers.clear()
        for timer in timers:
            timer.join(timeout=_GROK_THREAD_JOIN_SECONDS)

    # internals, under the lock

    @contextmanager
    def _acting(self) -> Generator[None, None, None]:
        """Change the world under its lock, then wake the watcher for a root-turn file change, as its path watcher does."""
        try:
            with self._lock:
                yield
        finally:
            with self._lock:
                is_changed = self._is_turn_files_changed
                self._is_turn_files_changed = False
            if is_changed:
                self.on_turn_files_changed()

    def _launch(self, *, is_gated: bool) -> None:
        """A grok launch: mngr resets its turn files and touches the process marker, and grok starts a fresh session."""
        self._process_count += 1
        for timer in self._timers:
            timer.cancel()
        get_root_turns_dir(self.dir).mkdir(parents=True, exist_ok=True)
        marker = self.dir / PROCESS_STARTED_MARKER_FILENAME
        marker.touch()
        launched_at_ns = _GROK_PROCESS_CLOCK_BASE_NS + self._process_count * 1_000_000_000
        os.utime(marker, ns=(launched_at_ns, launched_at_ns))
        write_live_grok_pid(self.dir)
        self.is_running = True
        self._reset_process_state(is_gated=is_gated)
        if not is_gated:
            self._start_session()
        self._recompute_active()

    def _reset_process_state(self, *, is_gated: bool) -> None:
        """Everything a grok process held, gone with it; mngr's own turn files go too."""
        for turn_path in get_root_turns_dir(self.dir).iterdir():
            turn_path.unlink()
        self.turn_prompt_id = None
        self.lagging_prompt_id = None
        self.placeholder_child_id = None
        self.grok_queue = []
        self.composer = ""
        self.quit_armed_at = None
        self.subagent_ids = []
        self.shell_ids = []
        self.due_wakes = []
        self.is_gated = is_gated
        self.session_id = ""
        self._is_turn_files_changed = True

    def _start_session(self) -> None:
        self.session_id = str(uuid4())
        add_grok_session(self.dir, self.session_id, b"", is_root=True)

    def _clear_gate(self) -> None:
        self.is_gated = False
        if not self.session_id:
            self._start_session()
        self._run_next_held_by_grok()

    def _log(self, update: dict[str, Any]) -> None:
        self._record_ms += 1
        append_grok_updates(self.dir, self.session_id, grok_update_line(self.session_id, update, self._record_ms))

    def _write_turn_file(self, prompt_id: str, tail: str) -> None:
        write_root_turn(self.dir, self.session_id, prompt_id, tail)
        self._is_turn_files_changed = True

    def _recompute_active(self) -> None:
        """mngr's ``active``: a top-level turn or a subagent is in flight; background shells do not count."""
        marker = self.dir / ACTIVE_MARKER_FILENAME
        if any(get_root_turns_dir(self.dir).iterdir()) or self.subagent_ids:
            marker.write_text("")
        else:
            marker.unlink(missing_ok=True)

    def _schedule(self, action: Callable[[], None]) -> None:
        timer = threading.Timer(_GROK_TURN_FILE_LAG_SECONDS, self._run_scheduled, args=(action,))
        self._timers.append(timer)
        timer.start()

    def _run_scheduled(self, action: Callable[[], None]) -> None:
        with self._acting():
            action()

    def _begin_turn(self, text: str, prompt_id: str, *, is_wake: bool) -> None:
        """grok starts a top-level turn, and mngr's UserPromptSubmit hook writes its file over any older one."""
        self._prompt_index += 1
        chunk_meta: dict[str, Any] = {"modelId": "stub", "promptIndex": self._prompt_index}
        if is_wake:
            chunk_meta["hideFromScrollback"] = True
        self._log(
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}, "_meta": chunk_meta}
        )
        if self.placeholder_child_id is not None and prompt_id != f"subagent-completed-{self.placeholder_child_id}":
            self.due_wakes.append(("subagent", self.placeholder_child_id))
        self.placeholder_child_id = None
        self.turn_prompt_id = prompt_id
        self.prompt_text_by_id[prompt_id] = text
        self.lagging_prompt_id = None
        self._write_turn_file(prompt_id, "<auto-wake>" if is_wake else "turn")
        self._recompute_active()

    def _start_user_turn(self, text: str, *, delivered_text: str | None = None) -> None:
        """grok commits a user turn: each accepted message it holds is Delivered, here and only here."""
        self._begin_turn(text, str(uuid4()), is_wake=False)
        accepted = set(self.ledger.accepted)
        lines = _block_texts(text if delivered_text is None else delivered_text)
        self.ledger.delivered.extend(line for line in lines if line in accepted)

    def _start_wake_turn(self, kind: str, task_id: str) -> None:
        """grok's wake turn for background work whose end it already logged."""
        prompt_id = f"task-completed-{task_id}" if kind == "shell" else f"subagent-completed-{task_id}"
        self._begin_turn(f"Background work {task_id} finished.", prompt_id, is_wake=True)
        self.stats["wake"] += 1

    def _end_shell_and_wake(self, task_id: str) -> None:
        """A background shell ends on an idle grok, whose wake turn starts at once with no placeholder before it."""
        self._log({"sessionUpdate": "task_completed", "task_snapshot": {"task_id": task_id}, "will_wake": True})
        self._start_wake_turn("shell", task_id)

    def _end_turn(self) -> None:
        prompt_id = self.turn_prompt_id
        if prompt_id is None:
            return
        self._log({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "STUB_REPLY: ok"}})
        self._log({"sessionUpdate": "turn_completed", "prompt_id": prompt_id, "stop_reason": "end_turn"})
        self.turn_prompt_id = None
        self.lagging_prompt_id = prompt_id
        self._run_next_held_by_grok()

    def _run_next_held_by_grok(self) -> bool:
        """An idle grok's next turn: what it queued, then the draft its pane's person sends, then a wake that is due."""
        if self.is_gated:
            return False
        if self.grok_queue:
            self._start_user_turn(self.grok_queue.pop(0))
            return True
        if self.composer:
            draft = self.composer
            self.composer = ""
            self._start_user_turn(draft)
            return True
        if self.due_wakes:
            kind, task_id = self.due_wakes.pop(0)
            self._start_wake_turn(kind, task_id)
            return True
        return False

    def _complete_turn_file(self, prompt_id: str) -> None:
        """mngr completes an ended turn: its file goes, unless a newer turn already wrote over it."""
        if self.lagging_prompt_id != prompt_id:
            return
        self.lagging_prompt_id = None
        (get_root_turns_dir(self.dir) / self.session_id).unlink(missing_ok=True)
        self._is_turn_files_changed = True
        self._recompute_active()

    def _take_ctrl_c(self) -> None:
        """grok's own answer to one Ctrl+C."""
        if self._is_quit_armed():
            raise AssertionError(
                f"FATAL: a Ctrl+C within {_GROK_QUIT_WINDOW_SECONDS}s of one that armed quit QUIT grok"
                f"\n{self.ledger.replay_note()}"
            )
        if self.composer:
            self.composer = ""
            return
        prompt_id = self.turn_prompt_id
        if prompt_id is None:
            self.quit_armed_at = self.clock
            self.stats["quit_armed"] += 1
            return
        self.stats["cancel"] += 1
        self._log({"sessionUpdate": "turn_completed", "prompt_id": prompt_id, "stop_reason": "cancelled"})
        self.turn_prompt_id = None
        self.lagging_prompt_id = prompt_id
        if not self._run_next_held_by_grok():
            self._schedule(lambda: self._complete_turn_file(prompt_id))

    def _check_send(self, text: str) -> None:
        """mngr's preflight: a gate fails the send as INPUT_BLOCKED, a turn or a placeholder as AGENT_BUSY."""
        lines = text.split("\n")
        if len(lines) > 1 and any(line.startswith("/") for line in lines):
            raise AssertionError(
                f"MERGED: a slash command was typed inside a block: {text!r}\n{self.ledger.replay_note()}"
            )
        if self.is_preflight_race_armed and not self.is_gated and self.turn_prompt_id is None:
            self.is_preflight_race_armed = False
            self.stats["race:preflight"] += 1
            self._end_shell_and_wake(self._next_id("shell"))
        pane = self.render_pane()
        gate_screen = detect_gate_screen(pane)
        if gate_screen is not None:
            self.stats["send_blocked"] += 1
            raise SendFailedError(f"grok shows its {gate_screen.lower()} screen", SendFailureKind.INPUT_BLOCKED.value)
        if get_root_turns(self.dir) or has_cancel_hint(pane):
            self.stats["send_refused_busy"] += 1
            raise SendFailedError("grok is running a turn", SendFailureKind.AGENT_BUSY.value)

    def _paste(self, text: str) -> None:
        """grok takes mngr's paste: a slash command runs at once, a message starts a turn or queues behind one."""
        self.is_last_paste_after_draft = False
        if self.is_paste_race_armed:
            self.is_paste_race_armed = False
            self.stats["race:paste"] += 1
            self._end_shell_and_wake(self._next_id("shell"))
        if _is_grok_control_command(text):
            verb, value = text.split(maxsplit=1)
            self.applied_by_verb[verb] = value
            self.stats["control_applied"] += 1
            self._write_model_state()
            return
        if text.startswith("/") and not self.composer:
            self.local_commands_run.append(text)
            self.ledger.delivered.append(text)
            self.stats["local_command"] += 1
            return
        if self.turn_prompt_id is None:
            # mngr warns about a draft left in the composer and types the message after it, so both go as one turn.
            draft = self.composer
            self.composer = ""
            if draft:
                self.delivered_text_by_drafted_turn[draft + text] = text
                self.is_last_paste_after_draft = True
                self.stats["draft_appended"] += 1
            self._start_user_turn(draft + text, delivered_text=text)
            return
        # grok queues the paste behind the turn that started after mngr's check; mngr's queued-row probe confirms it.
        self.grok_queue.append(text)
        self.stats["parked"] += 1
        if self.is_raced_wake_long:
            self.is_raced_wake_long = False
            self.stats["parked_long"] += 1
        else:
            # The raced wake turn ends inside the worker's witness window, and grok runs the paste as its own turn.
            self._end_turn()

    def _write_model_state(self) -> None:
        """mngr_grok's status line: what grok runs, rewritten once a switch applies."""
        state = {
            "model": self.applied_by_verb.get("/model"),
            "effort": self.applied_by_verb.get("/effort"),
            "fast": False,
        }
        get_grok_model_state_path(self.dir).write_text(json.dumps(state) + "\n")


class _GrokFlushThread:
    """The flush worker's own thread, woken by the watcher's wake as the real one is, while the driver blocks on a call."""

    def __init__(self, watcher: GrokSessionWatcher) -> None:
        self._watcher = watcher
        self._stopping = threading.Event()
        self._errors: list[AssertionError | MngrError | OSError] = []
        self._thread = threading.Thread(target=self._run, name="grok-storm-flush", daemon=True)

    def __enter__(self) -> None:
        self._thread.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopping.set()
        self._watcher._flush_wake.set()
        self._thread.join(timeout=_GROK_THREAD_JOIN_SECONDS)
        assert not self._thread.is_alive(), "the storm's flush thread never stopped"
        if self._errors:
            raise self._errors[0]

    def _run(self) -> None:
        wake = self._watcher._flush_wake
        while not self._stopping.is_set():
            wake.wait()
            if self._stopping.is_set():
                return
            wake.clear()
            try:
                self._watcher._attempt_flush()
            except (AssertionError, MngrError, OSError) as error:
                self._errors.append(error)
                return


class _GrokChat:
    """The chat app's real grok pieces for one agent, wired as AgentManager and the stop and tap endpoints wire them."""

    def __init__(self, world: _GrokWorld, *, delivery_witness_seconds: float = 0.0) -> None:
        self.world = world
        self.ledger = world.ledger
        agent_id = world.dir.name
        self.agent_info = make_grok_agent_info(world.dir, agent_id, agent_id)
        drop_grok_tracker(agent_id)
        self.turn_end_deadline_seconds = _GROK_TURN_END_DEADLINE_SECONDS
        self.tap_statuses: Counter[str] = Counter()
        self._published: list[str] = []
        self._published_lock = threading.Lock()
        self._early_turn_messages: list[str] = []
        # What the chat held when grok last exited: the relaunch's new session drops it (contract Part B).
        self._held_at_exit: list[str] = []
        # Breaches of the lifecycle's rules that leave every message accounted for, reported together at the end.
        self.violations: list[str] = []
        self._stuck_reported: set[str] = set()
        self.tracker = get_grok_tracker(world.dir)
        self.watcher = GrokSessionWatcher.build(self.agent_info, self._on_broadcast)
        self.watcher.set_flush_hooks(_grok_flush_hook_must_not_send, world.is_alive)
        self.watcher.delivery_witness_seconds = delivery_witness_seconds
        self.tracker.attach(publish=self._on_published, wake=self._on_tracker_wake)
        world.on_turn_files_changed = self.watcher._on_root_turns_changed
        self.session = GrokHarnessSession.build(
            SessionDeps(
                harness=HarnessType.GROK,
                state_dir=world.dir,
                model_state_path=get_grok_model_state_path(world.dir),
                send_to_harness=world.mngr_send,
                notify_agents_changed=_do_nothing,
                is_harness_starting_up=_is_never_true,
                is_tracked=_is_always_true,
                on_queue_snapshot=lambda _snapshot: None,
                on_user_turn=lambda _event: None,
                recompute_activity=_do_nothing,
                clear_queue_state=_do_nothing,
                catalog_options=lambda: (),
                build_interrupter=self._build_interrupter,
                build_shoulder_tap=self._build_shoulder_tap,
            )
        )
        self.session.send_result_wait_seconds = _GROK_SEND_RESULT_WAIT_SECONDS
        self.session.ensure_live()
        self.resolver = GrokModelResolver.build(self.agent_info)

    def close(self) -> None:
        self.world.join_timers()
        self.session.close()
        self.tracker.detach(self._on_published)
        drop_grok_tracker(self.agent_info.id)

    def _build_interrupter(self, agent_info: AgentInfo) -> InterruptToComposer:
        interrupter = GrokInterruptToComposer.build(agent_info)
        interrupter.turn_end_deadline_seconds = self.turn_end_deadline_seconds
        return interrupter

    def _build_shoulder_tap(self, agent_info: AgentInfo) -> AtomicShoulderTap:
        tap = GrokAtomicShoulderTap.build(agent_info)
        tap.turn_end_deadline_seconds = self.turn_end_deadline_seconds
        return tap

    def record_violation(self, message: str) -> None:
        last_round = next((op for op in reversed(self.ledger.ops) if op.startswith("round ")), "before round 0")
        last_op = self.ledger.ops[-1].strip() if self.ledger.ops else "setup"
        self.violations.append(f"{last_round.rstrip(':')}, at {last_op}: {message}")

    def assert_no_violations(self) -> None:
        assert not self.violations, "\n".join(
            ("lifecycle rule violations:", *self.violations, self.ledger.replay_note())
        )

    def _on_tracker_wake(self) -> None:
        """The tracker waking the flush worker, as the watcher's start binds it, with an armed wake turn starting first."""
        self.world.start_worker_race()
        self.watcher._flush_wake.set()

    def _on_published(self, snapshot: list[dict[str, Any]]) -> None:
        with self._published_lock:
            self._published = [str(entry["content"]) for entry in snapshot]

    def _on_broadcast(self, _agent_id: str, events: list[dict[str, Any]]) -> None:
        """Contract A3b: a delivered message's turn reaches the client only once its chip has left the queue."""
        with self._published_lock:
            chips = set(self._published)
        for event in events:
            if event.get("type") == "user_message" and event.get("role") == "user":
                content = str(event.get("content") or "")
                self._early_turn_messages.extend(line for line in _block_texts(content) if line in chips)

    # the operations

    def run_op(self, op: str, rng: random.Random) -> None:
        world = self.world
        world.advance()
        match op:
            case "send":
                self.send(world.new_text())
            case "race:worker":
                world.arm_worker_race()
                self.send(world.new_text())
            case "race:preflight":
                world.arm_preflight_race()
                self.send(world.new_text())
            case "race:paste":
                world.arm_paste_race()
                self.send(world.new_text())
            case "race:paste_long":
                world.arm_paste_race(is_wake_long=True)
                self.send(world.new_text())
            case "slash":
                self.send(world.new_command())
            case "idle_draft":
                world.pane_idle_draft()
            case "settle":
                self.settle()
            case "worker":
                self.watcher._attempt_flush()
            case "turn_ends":
                world.end_turn()
            case "streamer":
                world.catch_up_streamer()
            case "spawn_subagent":
                world.spawn_subagent()
            case "subagent_ends":
                world.end_subagent()
            case "wake_starts":
                world.start_pending_wake()
            case "spawn_shell":
                world.spawn_shell()
            case "shell_ends":
                world.end_shell()
            case "pane_turn":
                world.pane_turn()
            case "pane_draft":
                world.pane_draft()
            case "gate":
                world.toggle_gate()
            case "grok_exits":
                self._held_at_exit = [str(entry["content"]) for entry in self.tracker.snapshot()]
                world.exit_grok()
            case "model_switch":
                self.switch_model(rng)
            case "emit":
                self.watcher._emit_cycle()
            case "tap":
                self.tap()
            case "stop":
                self.stop()
            case "stop:stranded":
                world.strand_turn()
                self.stop()
            case "stop:restart_fails":
                world.strand_turn()
                world.is_next_restart_failing = True
                self.stop()
            case _:
                pytest.fail(f"unknown grok op {op}")
        self.drain_wakes()

    def send(self, text: str) -> None:
        """A POST to the real session: held at once behind a turn, or answered by the flush worker's first send."""
        self.ledger.accepted.append(text)
        with _GrokFlushThread(self.watcher):
            try:
                outcome = self.session.send(text, f"storm-{uuid4().hex}")
                if outcome is SendOutcome.NOT_READY:
                    # The route starts the agent and sends again.
                    self.world.start_grok()
                    outcome = self.session.send(text, f"storm-{uuid4().hex}")
            except SendFailedError as error:
                # A failed POST: the composer gets the text back, which is how the user sees it Returned.
                assert error.kind == SendFailureKind.INPUT_BLOCKED.value, (
                    f"only a gate fails a POST here, not {error.kind}: {error.detail}\n{self.ledger.replay_note()}"
                )
                self.ledger.returned.append(text)
                self.world.stats["post_failed"] += 1
                return
        assert outcome is SendOutcome.OK, f"the POST answered {outcome}\n{self.ledger.replay_note()}"

    def stop(self, *, is_worker_thread_started: bool = True) -> None:
        """The stop button through the real session, accounting for every message it hands back."""
        world = self.world
        note = self.ledger.replay_note
        is_stranded = world.is_turn_stranded()
        found_prompt_id = world.cancellable_prompt_id()
        restarts_before = world.restart_count
        delivered_before = len(self.ledger.delivered)
        # grok runs what it queued itself once the cancel lands; the chat no longer holds those messages.
        parked_before = {line for text in world.grok_queue for line in _block_texts(text)}
        self.turn_end_deadline_seconds = (
            _GROK_STRANDED_TURN_DEADLINE_SECONDS if is_stranded else _GROK_TURN_END_DEADLINE_SECONDS
        )
        world.catch_up_streamer_soon()
        if is_worker_thread_started:
            with _GrokFlushThread(self.watcher):
                block = self._interrupt()
        else:
            block = self._interrupt()
        world.join_timers()
        returned = _block_texts(block)
        assert not any(_is_grok_control_command(line) for line in returned), (
            f"stop returned a command: {returned}\n{note()}"
        )
        positions = [self.ledger.accepted.index(text) for text in returned if text in self.ledger.accepted]
        assert positions == sorted(positions), f"stop returned {returned} out of send order\n{note()}"
        delivered_by_stop = [text for text in self.ledger.delivered[delivered_before:] if text not in parked_before]
        assert not delivered_by_stop, f"stop delivered {delivered_by_stop} instead of returning them\n{note()}"
        killed_prompt_id = world.prompt_id_at_last_restart
        if world.restart_count > restarts_before and (found_prompt_id is None or killed_prompt_id != found_prompt_id):
            # The restart is the hammer for a turn stop could not end; any other restart kills a turn for nothing.
            self.record_violation(
                f"stop restarted grok with --no-resume although the turn it found "
                f"({world.prompt_text_by_id.get(found_prompt_id or '', 'none')!r}) had ended; the turn it killed was "
                f"{world.prompt_text_by_id.get(killed_prompt_id or '', 'none')!r}, which grok started after the cancel"
            )
        self.ledger.returned.extend(returned)

    def _interrupt(self) -> str:
        return self.session.interrupt_to_composer(
            self.agent_info,
            self.watcher,
            self.world.restart_process,
            _do_nothing,
            self.world.press_ctrl_c,
            _is_never_true,
        )

    def tap(self) -> None:
        """The shoulder tap through the real session: it may cancel the turn, and it never hands anything back."""
        world = self.world
        self.turn_end_deadline_seconds = (
            _GROK_STRANDED_TURN_DEADLINE_SECONDS if world.is_turn_stranded() else _GROK_TURN_END_DEADLINE_SECONDS
        )
        world.catch_up_streamer_soon()
        with _GrokFlushThread(self.watcher):
            outcome = self.session.shoulder_tap(
                self.agent_info, self.watcher, world.press_ctrl_c, _grok_tap_must_not_send
            )
        world.join_timers()
        assert outcome.block == "", f"the tap handed back {outcome.block!r}\n{self.ledger.replay_note()}"
        self.tap_statuses[outcome.status] += 1

    def switch_model(self, rng: random.Random) -> None:
        """The model bar's switch through the real resolver, which holds both commands in the control lane."""
        model_id, effort = rng.choice(_GROK_MODEL_PICKS)
        result = self.resolver.switch(
            ModelIdentity(model_id=model_id, effort=effort, fast=False),
            frozenset({ModelAxis.MODEL, ModelAxis.EFFORT}),
            _grok_bar_must_not_type,
        )
        assert result.ok, result.detail
        self.world.requested_by_verb.update({"/model": model_id, "/effort": effort})

    def drain_wakes(self) -> None:
        """The flush worker answering each wake, as its thread does between the storm's ops."""
        wake = self.watcher._flush_wake
        for _ in range(_GROK_MAX_WAKES_PER_OP):
            if not wake.is_set():
                return
            wake.clear()
            self.watcher._attempt_flush()

    def settle(self) -> None:
        """grok finishes everything it is doing while the worker delivers what is held, as at the end of any turn."""
        for _ in range(_GROK_SETTLE_TICKS):
            if self.world.is_quiet() and not self.tracker.has_held_work():
                return
            self.world.settle_step()
            self.drain_wakes()
            self.watcher._attempt_flush()

    # the checks

    def assert_each_message_in_one_state(self, context: str) -> None:
        """Contract Part D, after every step: each accepted message is in exactly one state, and the chips are true."""
        note = self.ledger.replay_note
        snapshot = self.tracker.snapshot()
        held = [str(entry["content"]) for entry in snapshot]
        if self._held_at_exit and self.world.is_running:
            still_held = [text for text in self._held_at_exit if text in held]
            assert not still_held, f"{still_held} outlived the grok session they were queued for\n{note()}"
            settled = set(self.ledger.delivered + self.ledger.returned)
            dropped = [text for text in self._held_at_exit if text not in settled]
            self.ledger.killable.update(dropped)
            self.ledger.killed.extend(dropped)
            self._held_at_exit = []
        stuck = [
            str(entry["content"])
            for entry in snapshot
            if entry["is_sending"] and str(entry["content"]) not in self._stuck_reported
        ]
        if stuck:
            # Contract Send: a message resolves to Delivered, Queued or Returned and is never left Sending.
            self._stuck_reported.update(stuck)
            self.record_violation(f"STUCK Sending once its POST had answered: {stuck}")
        with self._published_lock:
            published = list(self._published)
        assert published == held, (
            f"the chips last published {published} are not the queue {held} after {context}\n{note()}"
        )
        assert not self._early_turn_messages, (
            f"A3b: {self._early_turn_messages} reached the transcript while still a chip, after {context}\n{note()}"
        )
        accepted = set(self.ledger.accepted)
        parked = [line for text in self.world.grok_queue for line in _block_texts(text) if line in accepted]
        placements = Counter(self.ledger.delivered + self.ledger.returned + self.ledger.killed + held + parked)
        misplaced = {text: placements[text] for text in self.ledger.accepted if placements[text] != 1}
        assert not misplaced, (
            f"not in exactly one state after {context} (0 = lost, 2 or more = a ghost or a duplicate): {misplaced}"
            f"\n  held={held} parked-in-grok={parked}\n{note()}"
        )
        strangers = [text for text in placements if text not in accepted]
        assert not strangers, f"never accepted, yet in a state after {context}: {strangers}\n{note()}"

    def assert_round_settled(self) -> None:
        note = self.ledger.replay_note
        assert not self.tracker.has_held_work(), (
            f"the queue or the control lane must drain once grok is quiet -- a held message that never arrives is "
            f"a strand, not conservation: {self.tracker.snapshot()} {self.tracker.next_control_command()!r}\n{note()}"
        )
        assert self.world.applied_by_verb == self.world.requested_by_verb, (
            f"LOST control command: grok runs {self.world.applied_by_verb}, the bar asked for "
            f"{self.world.requested_by_verb}\n{note()}"
        )

    def assert_the_transcript_holds_each_delivered_message_once(self) -> None:
        """The on-disk cross-check: user turns in grok's real session logs are exactly the delivered set, once each."""
        note = self.ledger.replay_note
        accepted = set(self.ledger.accepted)
        events = GrokTranscriptLoader.build_loader(self.agent_info).get_all_events()
        drafted = self.world.delivered_text_by_drafted_turn
        on_disk = Counter(
            drafted.get(line, line)
            for event in events
            if event.get("type") == "user_message" and event.get("role") == "user"
            for line in _block_texts(str(event.get("content") or ""))
            if drafted.get(line, line) in accepted
        )
        local_commands = Counter(self.world.local_commands_run)
        delivered_turns = Counter(text for text in self.ledger.delivered if text not in local_commands)
        assert on_disk == delivered_turns, (
            f"user turns on disk {on_disk} != the delivered ledger {delivered_turns}\n{note()}"
        )
        assert all(count == 1 for count in local_commands.values()), (
            f"grok ran a command twice: {local_commands}\n{note()}"
        )
        order = [self.ledger.accepted.index(text) for text in self.ledger.delivered]
        assert order == sorted(order), f"messages were delivered out of send order: {self.ledger.delivered}\n{note()}"


def test_grok_conservation_storm_stop_and_tap_executors(tmp_path: Path) -> None:
    """Seeded rounds of grok sends, turns, background work, gates, model switches, stops and taps.

    The property is the other storms' (contract A1), checked after every op rather than per round: each accepted
    message is Delivered, Returned, held (Queued) or parked inside grok, and in exactly one of them. grok's specific
    risks are the worker typing into a turn mngr still reports (its files lag grok), stop or the tap pressing Ctrl+C
    into an idle grok (a second press quits it), a restart that loses what it took, a model switch merged into a block,
    handed back by stop, or dropped, and a message grok already has typed again: a slash command it ran, one it parked
    behind a turn, or one typed after a draft. Breaches that leave every message accounted for -- a message left
    Sending, a restart after a cancel that landed -- are collected and reported together once the rounds are done.
    """
    ledger = _Ledger("grok")
    world = _GrokWorld(tmp_path / f"agent-{uuid4().hex}", ledger)
    chat = _GrokChat(world)
    try:
        for round_index in range(_ROUND_COUNT):
            rng = random.Random(_BASE_SEED + round_index)
            ledger.log(f"round {round_index}:")
            if world.is_quiet():
                ledger.log("  kickoff-send")
                chat.run_op("send", rng)
            random_ops = tuple(rng.choice(_GROK_STORM_OPS) for _ in range(rng.randint(3, 6)))
            for op in (*_GROK_STAGED_ROUNDS.get(round_index, ()), *random_ops):
                ledger.log(f"  {op}")
                chat.run_op(op, rng)
                chat.assert_each_message_in_one_state(op)
            chat.settle()
            chat.assert_each_message_in_one_state("the round's settle")
            chat.assert_round_settled()
            ledger.verify()
        chat.assert_the_transcript_holds_each_delivered_message_once()
        assert world.stats["quit_armed"] == 0, f"mngr pressed Ctrl+C into an idle grok\n{ledger.replay_note()}"
        missing_paths = [path for path in _GROK_REQUIRED_PATHS if not world.stats[path]]
        missing_paths.extend(
            f"tap:{status}" for status in ("flushed", "nothing_queued") if not chat.tap_statuses[status]
        )
        assert not missing_paths, (
            f"the storm never reached {missing_paths}: reseed or reweight it\n{ledger.replay_note()}"
        )
        chat.assert_no_violations()
    finally:
        chat.close()


@pytest.mark.parametrize("is_flush_slow", (False, True))
def test_grok_interrupt_during_a_flush_conserves_every_message(tmp_path: Path, is_flush_slow: bool) -> None:
    """Contract Part D's required case: stop lands while the flush worker's send holds mngr's message lock.

    - fast: the paste has started the block's turn and mngr is still confirming it under the lock, so stop's Ctrl+C
      waits on the lock and then cancels a turn whose messages are already Delivered.
    - slow: the send holds the lock and has not pasted yet, so stop finds no turn, presses nothing and leaves the
      claimed block alone; the paste lands after stop has returned.

    Either way stop hands back nothing: the claimed block is the flush's to settle, and it lands exactly once.
    """
    ledger = _Ledger(f"grok-interrupt-during-flush[slow={is_flush_slow}]")
    world = _GrokWorld(tmp_path / f"agent-{uuid4().hex}", ledger)
    chat = _GrokChat(world, delivery_witness_seconds=_GROK_STEP_WAIT_SECONDS)
    try:
        world.pane_turn()
        for _ in range(2):
            chat.send(world.new_text())
        world.end_turn()
        chat.drain_wakes()
        chat.assert_each_message_in_one_state("two messages held behind a lagging turn file")
        world.paste_gate = threading.Event()
        world.is_confirm_held_for_press = not is_flush_slow
        with _GrokFlushThread(chat.watcher):
            world.catch_up_streamer()
            wait_for(world.send_holds_lock.is_set, timeout=_GROK_STEP_WAIT_SECONDS, poll_interval=0.01)
            if not is_flush_slow:
                world.paste_gate.set()
                wait_for(lambda: world.turn_prompt_id is not None, timeout=_GROK_STEP_WAIT_SECONDS)
            chat.stop(is_worker_thread_started=False)
            world.paste_gate.set()
            wait_for(lambda: not chat.tracker.has_entries(), timeout=_GROK_STEP_WAIT_SECONDS, poll_interval=0.01)
        chat.assert_each_message_in_one_state("stop during the flush")
        assert ledger.returned == [], "stop must not hand back a block whose flush it left running"
        assert ledger.delivered == ledger.accepted, "both messages reach grok exactly once, as one block"
        assert world.stats["cancel"] == (0 if is_flush_slow else 1)
        world.end_turn()
        world.catch_up_streamer()
        chat.drain_wakes()
        ledger.verify()
        chat.assert_the_transcript_holds_each_delivered_message_once()
        chat.assert_no_violations()
    finally:
        chat.close()


def test_grok_tap_delivers_once_and_returns_nothing(tmp_path: Path) -> None:
    """A tap cancels the turn and hands nothing back to the composer: the flush worker delivers the held block once."""
    ledger = _Ledger("grok-tap-handback")
    world = _GrokWorld(tmp_path / f"agent-{uuid4().hex}", ledger)
    chat = _GrokChat(world)
    try:
        world.pane_turn()
        for _ in range(2):
            chat.send(world.new_text())

        outcome = chat.session.shoulder_tap(chat.agent_info, chat.watcher, world.press_ctrl_c, _grok_tap_must_not_send)
        world.join_timers()

        assert (outcome.status, outcome.block) == ("flushed", ""), "nothing goes back to the composer on a tap"
        assert [str(entry["content"]) for entry in chat.tracker.snapshot()] == ledger.accepted, (
            "the queue is still the worker's to deliver"
        )
        assert world.stats["cancel"] == 1
        chat.drain_wakes()
        assert ledger.delivered == ledger.accepted
        ledger.verify()
        assert not chat.tracker.has_entries()
        chat.assert_no_violations()
    finally:
        chat.close()
