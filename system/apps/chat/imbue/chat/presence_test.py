import itertools

from imbue.chat.presence import PRESENCE_EXPIRY_SECONDS
from imbue.chat.presence import PresenceReport
from imbue.chat.presence import PresenceState
from imbue.chat.presence import PresenceTracker
from imbue.chat.presence import PresenceTransition
from imbue.chat.presence import WATCHING_STALE_SECONDS
from imbue.chat.primitives import ChatId

_CHAT = ChatId("agent-1")

# Unless a test numbers a report itself, every report is sent after the one before it.
_SEQUENCE_NUMBERS = itertools.count(1)


class _Clock:
    """A settable wall clock."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _tracker() -> tuple[PresenceTracker, _Clock]:
    clock = _Clock()
    return PresenceTracker(clock=clock), clock


def _report(
    instance_id: str,
    state: PresenceState,
    is_focused: bool = False,
    client_id: str = "client-a",
    sequence: int | None = None,
) -> PresenceReport:
    return PresenceReport(
        instance_id=instance_id,
        client_id=client_id,
        state=state,
        is_focused=is_focused,
        sequence=next(_SEQUENCE_NUMBERS) if sequence is None else sequence,
    )


def test_a_visible_report_makes_the_chat_open_and_visible() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE))
    assert tracker.is_open(_CHAT)
    assert tracker.is_visible(_CHAT)
    assert tracker.open_chat_ids() == {_CHAT}
    assert tracker.visible_chat_ids() == {_CHAT}


def test_a_hidden_report_is_open_but_not_visible() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.HIDDEN))
    assert tracker.is_open(_CHAT)
    assert not tracker.is_visible(_CHAT)
    assert tracker.visible_chat_ids() == set()


def test_a_closed_report_leaves_the_chat_neither_open_nor_watched() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True))
    tracker.record(_CHAT, _report("page-1", PresenceState.CLOSED))
    assert not tracker.is_open(_CHAT)
    assert not tracker.is_visible(_CHAT)
    assert tracker.watchers(_CHAT) == []
    assert tracker.open_chat_ids() == set()


def test_a_report_that_arrives_after_a_later_one_from_its_page_is_dropped() -> None:
    # The page's handshake sends hidden and shown sends visible a moment later, as two requests the
    # server can record in either order. Recorded last, the hidden one would leave the page hidden.
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True, sequence=2))

    assert tracker.record(_CHAT, _report("page-1", PresenceState.HIDDEN, is_focused=True, sequence=1)) == (
        PresenceTransition(is_newly_visible=False, is_newly_watched=False)
    )
    assert tracker.is_visible(_CHAT)
    assert tracker.watchers(_CHAT) == ["page-1"]


def test_a_report_that_arrives_after_its_page_closed_does_not_reopen_it() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.HIDDEN, sequence=1))
    tracker.record(_CHAT, _report("page-1", PresenceState.CLOSED, sequence=3))

    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True, sequence=2))

    assert not tracker.is_open(_CHAT)
    assert not tracker.is_visible(_CHAT)
    assert tracker.watchers(_CHAT) == []
    assert tracker.open_chat_ids() == set()


def test_closing_an_unknown_chat_is_a_no_op() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.CLOSED))
    assert tracker.open_chat_ids() == set()


def test_two_pages_of_one_client_keep_separate_reports() -> None:
    # A pulled-out chat: its copy in the main window is hidden while the popout shows it. Keyed by
    # client, whichever reported last would win; keyed by page, both stand.
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("popout-page", PresenceState.VISIBLE, is_focused=True))
    tracker.record(_CHAT, _report("main-window-page", PresenceState.HIDDEN))

    assert tracker.is_visible(_CHAT)
    assert tracker.watchers(_CHAT) == ["popout-page"]

    tracker.record(_CHAT, _report("main-window-page", PresenceState.CLOSED))
    assert tracker.watchers(_CHAT) == ["popout-page"]


def test_instances_aggregate_per_chat() -> None:
    # Visible in any page makes the chat visible; open until every page closes it.
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("desktop", PresenceState.HIDDEN))
    tracker.record(_CHAT, _report("phone", PresenceState.VISIBLE))
    assert tracker.is_visible(_CHAT)
    tracker.record(_CHAT, _report("phone", PresenceState.HIDDEN))
    assert tracker.is_open(_CHAT)
    assert not tracker.is_visible(_CHAT)
    tracker.record(_CHAT, _report("phone", PresenceState.CLOSED))
    assert tracker.is_open(_CHAT)
    tracker.record(_CHAT, _report("desktop", PresenceState.CLOSED))
    assert not tracker.is_open(_CHAT)


def test_only_a_visible_and_focused_page_watches() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("shown-unfocused", PresenceState.VISIBLE, is_focused=False))
    tracker.record(_CHAT, _report("hidden-focused", PresenceState.HIDDEN, is_focused=True))
    assert tracker.watchers(_CHAT) == []

    tracker.record(_CHAT, _report("shown-focused", PresenceState.VISIBLE, is_focused=True))
    assert tracker.watchers(_CHAT) == ["shown-focused"]


def test_every_watching_page_is_listed() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("page-b", PresenceState.VISIBLE, is_focused=True, client_id="laptop"))
    tracker.record(_CHAT, _report("page-a", PresenceState.VISIBLE, is_focused=True, client_id="phone"))
    assert tracker.watchers(_CHAT) == ["page-a", "page-b"]
    assert tracker.watchers(ChatId("agent-2")) == []


def test_a_watcher_goes_stale_long_before_its_report_expires() -> None:
    tracker, clock = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True))

    clock.now += WATCHING_STALE_SECONDS - 1.0
    assert tracker.watchers(_CHAT) == ["page-1"]

    clock.now += 1.0
    assert tracker.watchers(_CHAT) == []
    # Still well inside the ten-minute expiry the prioritizer reads.
    assert tracker.is_visible(_CHAT)
    assert tracker.is_open(_CHAT)


def test_a_heartbeat_keeps_a_watcher_watching() -> None:
    tracker, clock = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True))
    clock.now += WATCHING_STALE_SECONDS - 1.0
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True))
    clock.now += WATCHING_STALE_SECONDS - 1.0
    assert tracker.watchers(_CHAT) == ["page-1"]


def test_an_unrefreshed_report_expires() -> None:
    tracker, clock = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE))
    clock.now += PRESENCE_EXPIRY_SECONDS + 1.0
    assert not tracker.is_open(_CHAT)
    assert not tracker.is_visible(_CHAT)
    assert tracker.open_chat_ids() == set()


def test_a_heartbeat_refreshes_the_report() -> None:
    tracker, clock = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE))
    clock.now += PRESENCE_EXPIRY_SECONDS - 1.0
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE))
    clock.now += PRESENCE_EXPIRY_SECONDS - 1.0
    assert tracker.is_visible(_CHAT)


def test_expiry_is_per_instance() -> None:
    tracker, clock = _tracker()
    tracker.record(_CHAT, _report("stale", PresenceState.VISIBLE))
    clock.now += PRESENCE_EXPIRY_SECONDS - 1.0
    tracker.record(_CHAT, _report("fresh", PresenceState.HIDDEN))
    clock.now += 2.0
    # The stale page's visible report has expired; the fresh page's hidden one stands.
    assert tracker.is_open(_CHAT)
    assert not tracker.is_visible(_CHAT)


def test_forgetting_a_chat_drops_every_report() -> None:
    tracker, _ = _tracker()
    tracker.record(_CHAT, _report("desktop", PresenceState.VISIBLE, is_focused=True))
    tracker.record(_CHAT, _report("phone", PresenceState.HIDDEN))
    tracker.record(ChatId("agent-2"), _report("desktop-2", PresenceState.VISIBLE))
    tracker.forget_chat(_CHAT)
    assert tracker.open_chat_ids() == {ChatId("agent-2")}
    assert tracker.watchers(_CHAT) == []


def test_a_report_says_when_it_makes_the_chat_watched_and_only_then() -> None:
    tracker, _ = _tracker()

    assert tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE)) == PresenceTransition(
        is_newly_visible=True, is_newly_watched=False
    )
    assert tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True)) == PresenceTransition(
        is_newly_visible=False, is_newly_watched=True
    )
    # The heartbeat of a page already watching, and a second page joining it, change nothing.
    assert not tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True)).is_newly_watched
    assert not tracker.record(_CHAT, _report("page-2", PresenceState.VISIBLE, is_focused=True)).is_newly_watched


def test_watching_again_after_every_watcher_left_is_a_new_transition() -> None:
    tracker, clock = _tracker()
    tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True))

    assert not tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=False)).is_newly_watched
    assert tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True)).is_newly_watched

    # A watcher whose heartbeats stopped long enough to go stale is gone too: its next report is a return.
    clock.now += WATCHING_STALE_SECONDS + 1.0
    assert tracker.record(_CHAT, _report("page-1", PresenceState.VISIBLE, is_focused=True)).is_newly_watched
