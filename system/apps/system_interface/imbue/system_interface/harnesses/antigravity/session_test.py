"""Unit tests for agy's hold-vs-type send decision."""

import fcntl
from pathlib import Path
from typing import Any

from imbue.system_interface.activity_state import ACTIVE_MARKER_FILENAME
from imbue.system_interface.harnesses.antigravity.model import ANTIGRAVITY_CATALOG
from imbue.system_interface.harnesses.antigravity.queue_tracker import AntigravityQueueTracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import OUTBOX_FILENAME
from imbue.system_interface.harnesses.antigravity.queue_tracker import drop_tracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import get_tracker
from imbue.system_interface.harnesses.antigravity.queue_tracker import session_token
from imbue.system_interface.harnesses.antigravity.session import AntigravityHarnessSession
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session import SessionDeps


def _session(state_dir: Path, sent: list[str]) -> AntigravityHarnessSession:
    state_dir.mkdir(parents=True, exist_ok=True)
    # Each test gets a fresh queue for this agent id.
    drop_tracker(state_dir.name)
    unused: Any = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unused"))
    return AntigravityHarnessSession.build(
        SessionDeps(
            harness=HarnessType.ANTIGRAVITY,
            state_dir=state_dir,
            model_state_path=state_dir / "model_state.json",
            send_to_harness=lambda text: (sent.append(text), True)[1],
            notify_agents_changed=lambda: None,
            is_tracked=lambda: True,
            on_queue_snapshot=lambda snapshot: None,
            on_user_turn=lambda event: None,
            recompute_activity=lambda: None,
            clear_queue_state=lambda: None,
            catalog_options=lambda: ANTIGRAVITY_CATALOG.options,
            build_interrupter=unused,
            build_shoulder_tap=lambda agent_info: None,
        )
    )


def _tracker(state_dir: Path) -> AntigravityQueueTracker:
    """The same instance the session and watcher share (see queue_tracker)."""
    return get_tracker(state_dir.name, state_dir / OUTBOX_FILENAME, session_token(state_dir))


def test_an_idle_agent_is_typed_into_directly(tmp_path: Path) -> None:
    """No marker means no turn is open, so agy can act on the message immediately."""
    sent: list[str] = []
    session = _session(tmp_path / "agent-idle", sent)
    assert session.send("beep", "m1") is SendOutcome.OK
    assert sent == ["beep"]


def test_a_busy_agent_holds_the_message_instead_of_typing(tmp_path: Path) -> None:
    """The whole point: typing here would park the message invisibly inside agy's TUI, where
    it is merged into one turn and can never be resolved back."""
    sent: list[str] = []
    state_dir = tmp_path / "agent-busy"
    state_dir.mkdir(parents=True)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session = _session(state_dir, sent)
    assert session.send("beep", "m1") is SendOutcome.OK
    assert sent == [], "a message must NOT be typed into a busy agy"
    assert [entry["content"] for entry in session.switch_queue_snapshot()] == ["beep"]


def test_an_in_flight_send_means_busy_without_reading_the_marker(tmp_path: Path) -> None:
    """A held message lock IS an in-flight send, so the agent is busy by definition -- even
    before its marker has appeared. This is what makes two rapid sends safe."""
    sent: list[str] = []
    state_dir = tmp_path / "agent-inflight"
    state_dir.mkdir(parents=True)
    lock_path = state_dir / MESSAGE_LOCK_FILENAME
    session = _session(state_dir, sent)
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        outcome = session.send("bop", "m2")
    assert outcome is SendOutcome.OK
    assert sent == [], "a send in flight means busy; the message must be held"
    assert [entry["content"] for entry in session.switch_queue_snapshot()] == ["bop"]


def test_held_messages_keep_send_order(tmp_path: Path) -> None:
    sent: list[str] = []
    state_dir = tmp_path / "agent-order"
    state_dir.mkdir(parents=True)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session = _session(state_dir, sent)
    session.send("first", "m1")
    session.send("second", "m2")
    assert [entry["content"] for entry in session.switch_queue_snapshot()] == ["first", "second"]
    assert (state_dir / OUTBOX_FILENAME).exists(), "the queue must survive a backend restart"


def test_the_tap_is_withheld_while_a_flush_is_in_flight(tmp_path: Path) -> None:
    """Contract Shoulder-tap: available iff something is queued AND nothing is Sending.

    Without this the button stays lit through the flush, and pressing it would ctrl+c the
    very turn the flush is committing our block into, then re-send that same block.
    """
    sent: list[str] = []
    state_dir = tmp_path / "agent-flushing"
    state_dir.mkdir(parents=True)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    assert session.is_sending() is False
    assert session.is_tap_available(has_queued=True) is True

    _, claimed = _tracker(state_dir).begin_flush()
    assert session.is_sending() is True
    assert session.is_tap_available(has_queued=True) is False, "the tap must be withheld mid-flush"

    _tracker(state_dir).finish_flush(claimed, is_delivered=True)
    assert session.is_sending() is False


def test_in_flight_entries_are_not_returned_twice(tmp_path: Path) -> None:
    """A claimed entry stays IN the queue (that is what keeps it on screen), so stop already
    returns it via the queued block. in_flight_block must stay empty or it doubles."""
    sent: list[str] = []
    state_dir = tmp_path / "agent-double"
    state_dir.mkdir(parents=True)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    tracker = _tracker(state_dir)
    block, _claimed = tracker.begin_flush()
    assert block == "beep"
    assert tracker.concatenated_block() == "beep", "a claimed entry is still in the queue"
    assert session.in_flight_block() == "", "would be concatenated with the queued block"
