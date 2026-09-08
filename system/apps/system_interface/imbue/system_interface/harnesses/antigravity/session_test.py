"""Unit tests for agy's send: it enqueues, always, and never types."""

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
from imbue.system_interface.harnesses.antigravity.turn_state import drop_turn_state
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session import SessionDeps


def _session(state_dir: Path, sent: list[str]) -> AntigravityHarnessSession:
    state_dir.mkdir(parents=True, exist_ok=True)
    # Each test gets a fresh queue and reading for this agent id.
    drop_tracker(state_dir.name)
    drop_turn_state(state_dir.name)
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


def _queued(session: AntigravityHarnessSession) -> list[str]:
    return [str(entry["content"]) for entry in session._queue().snapshot()]


def test_an_idle_agent_is_still_only_enqueued(tmp_path: Path) -> None:
    """One typist. The session never types, even into an agent that is plainly free -- that
    decision is what had a window wide enough to land a message in a turn that just started."""
    sent: list[str] = []
    session = _session(tmp_path / "agent-idle", sent)
    assert session.send("beep", "m1") is SendOutcome.OK
    assert sent == []
    assert _queued(session) == ["beep"]


def test_a_busy_agent_holds_the_message(tmp_path: Path) -> None:
    sent: list[str] = []
    state_dir = tmp_path / "agent-busy"
    state_dir.mkdir(parents=True)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session = _session(state_dir, sent)
    assert session.send("beep", "m1") is SendOutcome.OK
    assert sent == []
    assert _queued(session) == ["beep"]


def test_held_messages_keep_send_order(tmp_path: Path) -> None:
    sent: list[str] = []
    state_dir = tmp_path / "agent-order"
    session = _session(state_dir, sent)
    session.send("first", "m1")
    session.send("second", "m2")
    assert _queued(session) == ["first", "second"]


def test_a_send_journals_so_it_survives_a_backend_restart(tmp_path: Path) -> None:
    sent: list[str] = []
    state_dir = tmp_path / "agent-journal"
    state_dir.mkdir(parents=True)
    (state_dir / "antigravity_process_started").write_text("")
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    assert (state_dir / OUTBOX_FILENAME).exists()


def test_the_tap_is_withheld_while_a_flush_is_in_flight(tmp_path: Path) -> None:
    """Contract Shoulder-tap: available iff something is queued AND nothing is Sending.

    Without this the button stays lit through the flush, and pressing it would ctrl+c the very
    turn the flush is committing our block into.
    """
    sent: list[str] = []
    state_dir = tmp_path / "agent-flushing"
    state_dir.mkdir(parents=True)
    # A turn must be open for the tap to be offered at all -- see is_tap_available.
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    assert session.is_sending() is False
    assert session.is_tap_available(has_queued=True) is True

    _block, claimed, generation = _tracker(state_dir).begin_flush()
    assert session.is_sending() is True
    assert session.is_tap_available(has_queued=True) is False, "the tap must be withheld mid-flush"

    _tracker(state_dir).finish_flush(claimed, generation, delivered=claimed)
    assert session.is_sending() is False


def test_in_flight_entries_are_not_returned_twice(tmp_path: Path) -> None:
    """A claimed entry stays IN the queue (that is what keeps it on screen), so stop already
    accounts for it there; returning it here as well would double it in the composer."""
    sent: list[str] = []
    state_dir = tmp_path / "agent-double"
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    tracker = _tracker(state_dir)
    block, _claimed, _generation = tracker.begin_flush()
    assert block == "beep"
    assert tracker.concatenated_block() == "beep", "a claimed entry is still in the queue"
    assert session.in_flight_block() == "", "nothing was typed, so nothing is in the registry"


def test_a_message_accepted_against_an_idle_agy_reads_as_sending(tmp_path: Path) -> None:
    """It is not parked -- the worker was woken inside the enqueue and is about to type it.

    Reporting Queued here told the user a message was waiting when it was already on its way,
    and flashed the shoulder-tap button on every ordinary send.
    """
    sent: list[str] = []
    state_dir = tmp_path / "agent-idle-queue"
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    entries = session._queue().snapshot()
    assert [e["is_sending"] for e in entries] == [True], "presented as Sending, not Queued"
    assert session.is_tap_available(has_queued=True) is False, "nothing for a tap to do"


def test_a_message_accepted_mid_turn_reads_as_queued(tmp_path: Path) -> None:
    """It really is parked behind a live turn, so a queued chip is the honest state."""
    sent: list[str] = []
    state_dir = tmp_path / "agent-mid-turn"
    state_dir.mkdir(parents=True)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("")
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    assert [e["is_sending"] for e in session._queue().snapshot()] == [False]


def test_a_failed_delivery_demotes_it_back_to_queued(tmp_path: Path) -> None:
    """Once an attempt has failed it is genuinely waiting, so it stops reading as Sending."""
    sent: list[str] = []
    state_dir = tmp_path / "agent-failed"
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    tracker = _tracker(state_dir)
    _block, claimed, generation = tracker.begin_flush()
    tracker.finish_flush(claimed, generation, delivered=())
    assert [e["is_sending"] for e in session._queue().snapshot()] == [False]


def test_a_turn_opening_first_demotes_it_back_to_queued(tmp_path: Path) -> None:
    """The worker declined to take it because a turn opened in the gap, so it is parked now."""
    sent: list[str] = []
    state_dir = tmp_path / "agent-raced"
    session = _session(state_dir, sent)
    session.send("beep", "m1")
    assert [e["is_sending"] for e in session._queue().snapshot()] == [True]
    assert _tracker(state_dir).demote_pending() is True
    assert [e["is_sending"] for e in session._queue().snapshot()] == [False]
