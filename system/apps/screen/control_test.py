"""The lease state machine: the browser app's cases carried over, plus the Screen's human-to-human take and the
implicit host neko reports for an uncontrolled Screen."""

import threading
from pathlib import Path

from mock_messenger_test import RecordingMessenger
from screen.control import CLAIM_WINDOW_SECONDS
from screen.control import LEASE_IDLE_TTL_SECONDS
from screen.control import ScreenControl
from screen.data_types import AcquireStatus
from screen.data_types import ControlView
from screen.data_types import Controller
from screen.primitives import AgentId
from screen.primitives import NekoSessionId
from testing import wait_until


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _control(
    messenger: RecordingMessenger | None = None, clock: _Clock | None = None, record_path: Path | None = None
) -> tuple[ScreenControl, list[ControlView], list[tuple[str, str]]]:
    views: list[ControlView] = []
    handoffs: list[tuple[str, str]] = []
    control = ScreenControl(
        messenger=messenger or RecordingMessenger(),
        on_changed=views.append,
        on_handoff_request=lambda name, reason: handoffs.append((name, reason)),
        record_path=record_path,
        clock=clock or _Clock(),
    )
    return control, views, handoffs


def _acquire(control: ScreenControl, agent: str, name: str | None = None, **overrides: object) -> AcquireStatus:
    arguments: dict[str, object] = {
        "is_reclaim": False,
        "is_wait": False,
        "max_wait_seconds": None,
        "is_enqueue_on_busy": False,
    }
    arguments.update(overrides)
    return control.acquire(AgentId(agent), name, **arguments)  # type: ignore[arg-type]


def _state(control: ScreenControl) -> tuple[Controller, str | None, bool, str | None]:
    view = control.view()
    return view.controller, view.agent_id, view.human_pinned, view.human_session_id


def test_acquire_release_is_compare_and_set() -> None:
    control, views, _ = _control()
    assert _acquire(control, "A", "Alice") == AcquireStatus.ACQUIRED
    assert _state(control) == (Controller.AGENT, "A", False, None)
    assert _acquire(control, "B", "Bob") == AcquireStatus.BUSY_AGENT
    assert _acquire(control, "A") == AcquireStatus.ACQUIRED
    assert control.release(AgentId("A")) is True
    assert _state(control) == (Controller.NOBODY, None, False, None)
    assert control.release(AgentId("A")) is False
    assert control.release(AgentId("B")) is False
    assert [view.controller for view in views] == [Controller.AGENT, Controller.NOBODY]


def test_take_control_preempts_pins_and_reclaim_resumes() -> None:
    control, _, _ = _control()
    _acquire(control, "A", "Alice")
    assert control.take_control(NekoSessionId("s1"), "Josh") is True
    assert _state(control) == (Controller.HUMAN, None, True, "s1")
    assert _acquire(control, "B", "Bob") == AcquireStatus.BUSY_HUMAN
    assert _acquire(control, "B", "Bob", is_wait=True, max_wait_seconds=0.1) == AcquireStatus.BUSY_HUMAN
    assert _acquire(control, "B", "Bob", is_reclaim=True) == AcquireStatus.ACQUIRED
    assert _state(control) == (Controller.AGENT, "B", False, None)


def test_enqueue_on_busy_queues_for_resume_and_wakes_on_handback() -> None:
    messenger = RecordingMessenger()
    control, _, _ = _control(messenger)
    _acquire(control, "A", "Alice")
    control.take_control(NekoSessionId("s1"), "Josh")
    assert _acquire(control, "A", "Alice", is_enqueue_on_busy=True) == AcquireStatus.BUSY_HUMAN
    assert control.view().waiting == ("Alice",)
    assert control.return_to_agents(NekoSessionId("s1"), is_owner=False) is True
    assert _state(control) == (Controller.AGENT, "A", False, None)
    assert control.view().waiting == ()
    assert [agent for agent, _ in messenger.messages] == ["A"]
    assert "handed back to you" in messenger.messages[0][1]


def test_agent_in_both_queues_is_not_re_granted_after_it_finishes() -> None:
    messenger = RecordingMessenger()
    control, _, _ = _control(messenger)
    _acquire(control, "A", "Alice")
    assert _acquire(control, "B", "Bob", is_enqueue_on_busy=True) == AcquireStatus.BUSY_AGENT
    assert control.view().waiting == ("Bob",)
    results: list[AcquireStatus] = []
    waiter = threading.Thread(
        target=lambda: results.append(_acquire(control, "B", "Bob", is_wait=True, max_wait_seconds=5.0))
    )
    waiter.start()
    assert wait_until(lambda: control.waiting_agent_ids() == ["B"])
    control.release(AgentId("A"))
    waiter.join(timeout=5.0)
    assert results == [AcquireStatus.ACQUIRED]
    assert _state(control) == (Controller.AGENT, "B", False, None)
    assert control.view().waiting == ()
    control.release(AgentId("B"))
    assert _state(control) == (Controller.NOBODY, None, False, None)
    assert messenger.messages == []


def test_human_pin_is_sticky_with_no_idle_yield() -> None:
    clock = _Clock()
    control, _, _ = _control(clock=clock)
    _acquire(control, "A", "Alice")
    control.take_control(NekoSessionId("s1"), "Josh")
    assert _acquire(control, "A", "Alice", is_enqueue_on_busy=True) == AcquireStatus.BUSY_HUMAN
    clock.now += LEASE_IDLE_TTL_SECONDS * 10
    assert control.sweep() is False
    assert _state(control) == (Controller.HUMAN, None, True, "s1")
    assert _acquire(control, "A", "Alice") == AcquireStatus.BUSY_HUMAN
    assert control.return_to_agents(NekoSessionId("s1"), is_owner=False) is True
    assert _state(control) == (Controller.AGENT, "A", False, None)


def test_return_to_agents_is_only_the_controlling_humans() -> None:
    control, _, _ = _control()
    _acquire(control, "A")
    assert control.return_to_agents(NekoSessionId("s1"), is_owner=True) is False
    control.release(AgentId("A"))
    assert control.return_to_agents(NekoSessionId("s1"), is_owner=True) is False
    control.take_control(NekoSessionId("s1"), "Josh")
    assert control.return_to_agents(NekoSessionId("other"), is_owner=True) is False
    assert control.return_to_agents(NekoSessionId("s1"), is_owner=False) is True
    assert _state(control) == (Controller.NOBODY, None, False, None)


def test_handoff_pins_the_human_fronts_resume_queue_and_announces() -> None:
    control, _, handoffs = _control()
    _acquire(control, "A", "Alice")
    assert _acquire(control, "B", "Bob", is_enqueue_on_busy=True) == AcquireStatus.BUSY_AGENT
    assert control.handoff(AgentId("A"), "Alice", "solve the CAPTCHA") is True
    assert _state(control) == (Controller.HUMAN, None, True, None)
    assert control.view().waiting == ("Alice", "Bob")
    assert handoffs == [("Alice", "solve the CAPTCHA")]
    # No agent takes a handed-off Screen, and the idle clock never frees it.
    assert _acquire(control, "C", "Carol", is_enqueue_on_busy=True) == AcquireStatus.BUSY_HUMAN
    assert control.sweep() is False
    # The human takes it and hands back: the requester resumes first.
    control.take_control(NekoSessionId("s1"), "Josh")
    assert _state(control) == (Controller.HUMAN, None, True, "s1")
    assert control.return_to_agents(NekoSessionId("s1"), is_owner=False) is True
    assert _state(control) == (Controller.AGENT, "A", False, None)


def test_the_first_input_after_a_handoff_claims_it_with_the_pin_kept() -> None:
    control, _, _ = _control()
    _acquire(control, "A", "Alice")
    assert control.handoff(AgentId("A"), "Alice", "sign in") is True
    assert control.human_became_host(NekoSessionId("s1"), "Josh") is True
    assert _state(control) == (Controller.HUMAN, None, True, "s1")
    assert _acquire(control, "B", "Bob", is_enqueue_on_busy=True) == AcquireStatus.BUSY_HUMAN
    # The claiming human's session leaving frees the Screen to the handoff's agent.
    assert control.human_session_gone(NekoSessionId("s1")) is True
    assert _state(control) == (Controller.AGENT, "A", False, None)


def test_the_owner_gives_an_unclaimed_handoff_back_and_a_visitor_cannot() -> None:
    control, _, _ = _control()
    _acquire(control, "A", "Alice")
    assert control.handoff(AgentId("A"), "Alice", "sign in") is True
    assert control.return_to_agents(NekoSessionId("visitor"), is_owner=False) is False
    assert _state(control) == (Controller.HUMAN, None, True, None)
    assert control.return_to_agents(NekoSessionId("owner"), is_owner=True) is True
    assert _state(control) == (Controller.AGENT, "A", False, None)


def test_handoff_is_a_noop_when_the_caller_does_not_hold_it() -> None:
    control, _, handoffs = _control()
    _acquire(control, "A", "Alice")
    assert control.handoff(AgentId("B"), "Bob", "x") is False
    assert _state(control) == (Controller.AGENT, "A", False, None)
    control.take_control(NekoSessionId("s1"), "Josh")
    assert control.handoff(AgentId("A"), "Alice", "x") is False
    assert handoffs == []


def test_unclaimed_grant_passes_on() -> None:
    clock = _Clock()
    messenger = RecordingMessenger()
    control, _, _ = _control(messenger, clock)
    _acquire(control, "A", "Alice")
    control.take_control(NekoSessionId("s1"), "Josh")
    _acquire(control, "A", "Alice", is_enqueue_on_busy=True)
    control.return_to_agents(NekoSessionId("s1"), is_owner=False)
    assert _state(control) == (Controller.AGENT, "A", False, None)
    assert control.is_grant_pending() is True
    clock.now += CLAIM_WINDOW_SECONDS + 1
    assert control.sweep() is True
    assert _state(control) == (Controller.NOBODY, None, False, None)


def test_touch_claims_a_grant_and_restarts_the_idle_clock() -> None:
    clock = _Clock()
    control, _, _ = _control(clock=clock)
    control.take_control(NekoSessionId("s1"), "Josh")
    _acquire(control, "A", "Alice", is_enqueue_on_busy=True)
    control.return_to_agents(NekoSessionId("s1"), is_owner=False)
    clock.now += CLAIM_WINDOW_SECONDS - 1
    control.touch(AgentId("A"))
    clock.now += CLAIM_WINDOW_SECONDS
    assert control.sweep() is False
    assert control.is_grant_pending() is False
    clock.now += LEASE_IDLE_TTL_SECONDS + 1
    assert control.sweep() is True
    assert _state(control) == (Controller.NOBODY, None, False, None)


def test_idle_lease_frees_itself_and_the_view_counts_down() -> None:
    clock = _Clock()
    control, _, _ = _control(clock=clock)
    _acquire(control, "A", "Alice")
    clock.now += 10
    assert control.view().idle_seconds == 10
    assert control.view().idle_release_seconds == int(LEASE_IDLE_TTL_SECONDS) - 10
    clock.now += LEASE_IDLE_TTL_SECONDS
    assert control.sweep() is True
    assert control.view().idle_seconds is None


def test_monitor_and_wait_hands_off_in_fifo_order() -> None:
    control, _, _ = _control()
    _acquire(control, "A", "Alice")
    order: list[tuple[str, AcquireStatus]] = []

    def waiter(name: str) -> None:
        order.append((name, _acquire(control, name, name, is_wait=True, max_wait_seconds=5.0)))

    thread_b = threading.Thread(target=waiter, args=("B",))
    thread_b.start()
    assert wait_until(lambda: control.waiting_agent_ids() == ["B"])
    thread_c = threading.Thread(target=waiter, args=("C",))
    thread_c.start()
    assert wait_until(lambda: control.waiting_agent_ids() == ["B", "C"])
    control.release(AgentId("A"))
    thread_b.join(timeout=5.0)
    assert _state(control) == (Controller.AGENT, "B", False, None)
    control.release(AgentId("B"))
    thread_c.join(timeout=5.0)
    assert _state(control) == (Controller.AGENT, "C", False, None)
    assert order == [("B", AcquireStatus.ACQUIRED), ("C", AcquireStatus.ACQUIRED)]


def test_wait_times_out_and_dequeues() -> None:
    control, _, _ = _control()
    _acquire(control, "A")
    assert _acquire(control, "Z", "Z", is_wait=True, max_wait_seconds=0.2) == AcquireStatus.TIMED_OUT
    assert control.waiting_agent_ids() == []


def test_take_control_evicts_waiters_and_queues_the_displaced_owner_first() -> None:
    messenger = RecordingMessenger()
    control, _, _ = _control(messenger)
    _acquire(control, "A", "Alice")
    results: list[AcquireStatus] = []
    waiter = threading.Thread(
        target=lambda: results.append(_acquire(control, "W", "W", is_wait=True, max_wait_seconds=5.0))
    )
    waiter.start()
    assert wait_until(lambda: control.waiting_agent_ids() == ["W"])
    control.take_control(NekoSessionId("s1"), "Josh")
    waiter.join(timeout=5.0)
    assert results == [AcquireStatus.BUSY_HUMAN]
    assert control.waiting_agent_ids() == []
    assert [queued.agent_id for queued in control.resume_queue()] == ["A"]
    control.return_to_agents(NekoSessionId("s1"), is_owner=False)
    assert _state(control) == (Controller.AGENT, "A", False, None)
    assert [agent for agent, _ in messenger.messages] == ["A"]


def test_abandon_frees_everyone_and_messages_the_queued() -> None:
    messenger = RecordingMessenger()
    control, _, _ = _control(messenger)
    _acquire(control, "A")
    results: list[AcquireStatus] = []
    waiter = threading.Thread(
        target=lambda: results.append(_acquire(control, "B", "B", is_wait=True, max_wait_seconds=5.0))
    )
    waiter.start()
    assert wait_until(lambda: control.waiting_agent_ids() == ["B"])
    _acquire(control, "C", "C", is_enqueue_on_busy=True)
    control.abandon("screen-xorg exited")
    waiter.join(timeout=5.0)
    assert results == [AcquireStatus.BUSY_HUMAN]
    assert _state(control) == (Controller.NOBODY, None, False, None)
    assert control.view().waiting == ()
    assert [agent for agent, _ in messenger.messages] == ["C"]
    assert "screen-xorg exited" in messenger.messages[0][1]


def test_acquire_denied_by_human_enqueues_when_requested() -> None:
    control, _, _ = _control()
    control.take_control(NekoSessionId("s1"), "Josh")
    assert _acquire(control, "B", "B", is_enqueue_on_busy=True) == AcquireStatus.BUSY_HUMAN
    assert [queued.agent_id for queued in control.resume_queue()] == ["B"]


def test_human_takes_control_from_another_human() -> None:
    control, views, _ = _control()
    control.take_control(NekoSessionId("s1"), "Josh")
    assert control.take_control(NekoSessionId("s2"), "Mia") is True
    assert _state(control) == (Controller.HUMAN, None, True, "s2")
    assert control.view().human_name == "Mia"
    # The displaced human cannot give back what is no longer theirs.
    assert control.return_to_agents(NekoSessionId("s1"), is_owner=True) is False
    assert control.return_to_agents(NekoSessionId("s2"), is_owner=False) is True
    assert [view.human_session_id for view in views] == ["s1", "s2", None]


def test_nobody_to_implicit_host_records_the_human_unpinned() -> None:
    control, _, _ = _control()
    assert control.human_became_host(NekoSessionId("s1"), "Josh") is True
    assert _state(control) == (Controller.HUMAN, None, False, "s1")
    # The same host again changes nothing; an agent is refused meanwhile.
    assert control.human_became_host(NekoSessionId("s1"), "Josh") is False
    assert _acquire(control, "A", "Alice", is_enqueue_on_busy=True) == AcquireStatus.BUSY_HUMAN
    # The human's session goes away: the queued agent resumes.
    assert control.human_session_gone(NekoSessionId("s1")) is True
    assert _state(control) == (Controller.AGENT, "A", False, None)


def test_implicit_host_is_ignored_while_an_agent_holds_the_lease() -> None:
    control, _, _ = _control()
    _acquire(control, "A", "Alice")
    assert control.human_became_host(NekoSessionId("s1"), "Josh") is False
    assert _state(control) == (Controller.AGENT, "A", False, None)


def test_lease_is_checkpointed_and_restored(tmp_path: Path) -> None:
    record_path = tmp_path / "control.json"
    control, _, _ = _control(record_path=record_path)
    _acquire(control, "A", "Alice")
    _acquire(control, "B", "Bob", is_enqueue_on_busy=True)
    restored, _, _ = _control(record_path=record_path)
    restored.restore()
    assert _state(restored) == (Controller.AGENT, "A", False, None)
    assert restored.view().waiting == ("Bob",)
    assert restored.view().agent_name == "Alice"


def test_restore_ignores_an_unreadable_checkpoint(tmp_path: Path) -> None:
    record_path = tmp_path / "control.json"
    record_path.write_text("{not json")
    control, _, _ = _control(record_path=record_path)
    control.restore()
    assert _state(control) == (Controller.NOBODY, None, False, None)
