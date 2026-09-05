from imbue.system_interface.presence import PRESENCE_EXPIRY_SECONDS
from imbue.system_interface.presence import PresenceState
from imbue.system_interface.presence import PresenceTracker


class _Clock:
    """A settable wall clock."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _tracker() -> tuple[PresenceTracker, _Clock]:
    clock = _Clock()
    return PresenceTracker(clock=clock), clock


def test_a_visible_report_makes_the_chat_open_and_visible() -> None:
    tracker, _ = _tracker()
    tracker.record("agent-1", "client-a", PresenceState.VISIBLE)
    assert tracker.is_open("agent-1")
    assert tracker.is_visible("agent-1")
    assert tracker.open_agent_ids() == {"agent-1"}
    assert tracker.visible_agent_ids() == {"agent-1"}


def test_a_hidden_report_is_open_but_not_visible() -> None:
    tracker, _ = _tracker()
    tracker.record("agent-1", "client-a", PresenceState.HIDDEN)
    assert tracker.is_open("agent-1")
    assert not tracker.is_visible("agent-1")
    assert tracker.visible_agent_ids() == set()


def test_a_closed_report_drops_the_clients_presence() -> None:
    tracker, _ = _tracker()
    tracker.record("agent-1", "client-a", PresenceState.VISIBLE)
    tracker.record("agent-1", "client-a", PresenceState.CLOSED)
    assert not tracker.is_open("agent-1")
    assert tracker.open_agent_ids() == set()


def test_closing_an_unknown_chat_is_a_no_op() -> None:
    tracker, _ = _tracker()
    tracker.record("agent-1", "client-a", PresenceState.CLOSED)
    assert tracker.open_agent_ids() == set()


def test_clients_aggregate_per_chat() -> None:
    # Visible in any client makes the chat visible; open until every client closes it.
    tracker, _ = _tracker()
    tracker.record("agent-1", "desktop", PresenceState.HIDDEN)
    tracker.record("agent-1", "phone", PresenceState.VISIBLE)
    assert tracker.is_visible("agent-1")
    tracker.record("agent-1", "phone", PresenceState.HIDDEN)
    assert tracker.is_open("agent-1")
    assert not tracker.is_visible("agent-1")
    tracker.record("agent-1", "phone", PresenceState.CLOSED)
    assert tracker.is_open("agent-1")
    tracker.record("agent-1", "desktop", PresenceState.CLOSED)
    assert not tracker.is_open("agent-1")


def test_an_unrefreshed_report_expires() -> None:
    tracker, clock = _tracker()
    tracker.record("agent-1", "client-a", PresenceState.VISIBLE)
    clock.now += PRESENCE_EXPIRY_SECONDS + 1.0
    assert not tracker.is_open("agent-1")
    assert not tracker.is_visible("agent-1")
    assert tracker.open_agent_ids() == set()


def test_a_heartbeat_refreshes_the_report() -> None:
    tracker, clock = _tracker()
    tracker.record("agent-1", "client-a", PresenceState.VISIBLE)
    clock.now += PRESENCE_EXPIRY_SECONDS - 1.0
    tracker.record("agent-1", "client-a", PresenceState.VISIBLE)
    clock.now += PRESENCE_EXPIRY_SECONDS - 1.0
    assert tracker.is_visible("agent-1")


def test_expiry_is_per_client() -> None:
    tracker, clock = _tracker()
    tracker.record("agent-1", "stale", PresenceState.VISIBLE)
    clock.now += PRESENCE_EXPIRY_SECONDS - 1.0
    tracker.record("agent-1", "fresh", PresenceState.HIDDEN)
    clock.now += 2.0
    # The stale client's visible report has expired; the fresh client's hidden one stands.
    assert tracker.is_open("agent-1")
    assert not tracker.is_visible("agent-1")


def test_forgetting_a_chat_drops_every_report() -> None:
    tracker, _ = _tracker()
    tracker.record("agent-1", "desktop", PresenceState.VISIBLE)
    tracker.record("agent-1", "phone", PresenceState.HIDDEN)
    tracker.record("agent-2", "desktop", PresenceState.VISIBLE)
    tracker.forget_agent("agent-1")
    assert tracker.open_agent_ids() == {"agent-2"}
