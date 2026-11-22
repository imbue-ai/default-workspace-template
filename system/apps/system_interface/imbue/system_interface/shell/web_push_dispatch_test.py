import json
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from py_vapid import Vapid
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.web_push import BrowserPushSubscription
from imbue.system_interface.shell.web_push import DeviceRegistrationRequest
from imbue.system_interface.shell.web_push import NotificationSettings
from imbue.system_interface.shell.web_push import PushProviderAnswer
from imbue.system_interface.shell.web_push import WebPushNotifier
from imbue.system_interface.shell.web_push_dispatch import COALESCE_SECONDS
from imbue.system_interface.shell.web_push_dispatch import FULL_TEXT_MAX_LENGTH
from imbue.system_interface.shell.web_push_dispatch import MAX_SEND_LOG_ENTRIES
from imbue.system_interface.shell.web_push_dispatch import PER_CHAT_POST_LIMIT
from imbue.system_interface.shell.web_push_dispatch import PER_CHAT_WINDOW_SECONDS
from imbue.system_interface.shell.web_push_dispatch import PushDispatcher
from imbue.system_interface.shell.web_push_dispatch import PushSendRequest
from imbue.system_interface.shell.web_push_dispatch import SendLog
from imbue.system_interface.shell.web_push_dispatch import SendStatus
from imbue.system_interface.shell.web_push_dispatch import WORKSPACE_POST_LIMIT
from imbue.system_interface.shell.web_push_dispatch import WORKSPACE_WINDOW_SECONDS
from imbue.system_interface.shell.web_push_dispatch import compose_notification
from imbue.system_interface.testing import RecordingPushTransport

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
_CHAT = "agent-research"
_PREVIEW_SAFE = NotificationSettings(is_full_text=False)
_FULL_TEXT = NotificationSettings(is_full_text=True)
_CHAT_LINK = "/?open=chat%3A%2F%3Fchat%3Dagent-research"


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW.timestamp()

    def __call__(self) -> float:
        return self.now


def _dispatcher(
    tmp_path: Path, is_device_registered: bool = True, transport: RecordingPushTransport | None = None
) -> tuple[PushDispatcher, _Clock, RecordingPushTransport]:
    transport = transport if transport is not None else RecordingPushTransport()
    notifier = WebPushNotifier(directory=tmp_path, transport=transport)
    if is_device_registered:
        notifier.register(
            DeviceRegistrationRequest.model_validate(
                {
                    "subscription": {"endpoint": "https://web.push.apple.com/a", "keys": {"p256dh": "B", "auth": "s"}},
                    "label": "iPhone, Safari",
                }
            ),
            _NOW,
        )
    clock = _Clock()
    return (
        PushDispatcher(notifier=notifier, log=SendLog(path=tmp_path / "send_log.json"), clock=clock),
        clock,
        transport,
    )


def _send(message: str, chat_id: str = _CHAT, title: str | None = None) -> PushSendRequest:
    return PushSendRequest(
        chat_id=chat_id, chat_title="Research", title=title, message=message, app="chat", app_path=f"/?chat={chat_id}"
    )


def test_a_notification_names_the_chat_and_says_nothing_the_agent_said_by_default() -> None:
    notification = compose_notification(
        _CHAT, "Research", _CHAT_LINK, ("Your code is 123456",), "Studio", _PREVIEW_SAFE
    )

    assert notification.title == "Research"
    assert notification.body == "Has something for you in Studio."
    assert "123456" not in notification.body
    assert notification.path == _CHAT_LINK
    assert notification.tag == "chat-agent-research"


def test_the_longest_chat_id_a_send_takes_composes_a_notification() -> None:
    chat_id = "agent-" + "a" * 120
    request = _send("Report ready", chat_id=chat_id)

    notification = compose_notification(
        request.chat_id, "Research", _CHAT_LINK, ("Report ready",), "Studio", _PREVIEW_SAFE
    )

    assert notification.tag == f"chat-{chat_id}"


def test_a_send_whose_link_would_be_too_long_for_a_notification_is_refused() -> None:
    with pytest.raises(ValidationError, match="link"):
        PushSendRequest(
            chat_id=_CHAT, chat_title="Research", message="m", app="chat", app_path="/?q=" + "\u00e9" * 400
        )


def test_full_text_shows_the_agents_words_cut_short_for_a_lock_screen() -> None:
    short = compose_notification(_CHAT, "Research", _CHAT_LINK, ("Report ready",), "Studio", _FULL_TEXT)
    long = compose_notification(_CHAT, "Research", _CHAT_LINK, ("x" * 1000,), "Studio", _FULL_TEXT)

    assert short.body == "Report ready"
    assert len(long.body) == FULL_TEXT_MAX_LENGTH and long.body.endswith("...")


def test_a_burst_says_how_many_updates_it_folds() -> None:
    safe = compose_notification(_CHAT, "Research", _CHAT_LINK, ("one", "two", "three"), "Studio", _PREVIEW_SAFE)
    full = compose_notification(_CHAT, "Research", _CHAT_LINK, ("one", "two", "three"), "Studio", _FULL_TEXT)

    assert safe.body == "3 updates for you in Studio."
    assert full.body == "3 updates. Latest: three"


def test_a_chats_first_notification_goes_out_at_once_on_the_worker(tmp_path: Path) -> None:
    dispatcher, _clock, transport = _dispatcher(tmp_path)

    receipt = dispatcher.submit(_send("Report ready", title="Q4"), "Studio")

    assert receipt.status is SendStatus.QUEUED
    assert transport.posted == []
    dispatcher.flush_due()
    ((posted,),) = [transport.posted]
    assert json.loads(posted.payload)["title"] == "Research"
    assert json.loads(posted.payload)["path"] == "/?open=chat%3A%2F%3Fchat%3Dagent-research"
    (entry,) = dispatcher.log.entries()
    assert (entry.status, entry.requested_text, entry.shown_title) == (SendStatus.SENT, "Q4: Report ready", "Research")
    assert [delivery.status_code for delivery in entry.deliveries] == [201]


def test_a_burst_from_one_chat_waits_out_the_window_and_goes_as_one(tmp_path: Path) -> None:
    dispatcher, clock, transport = _dispatcher(tmp_path)
    first = dispatcher.submit(_send("one"), "Studio")
    dispatcher.flush_due()

    clock.now += 5
    second = dispatcher.submit(_send("two"), "Studio")
    third = dispatcher.submit(_send("three"), "Renamed Studio")
    dispatcher.flush_due()
    assert len(transport.posted) == 1

    clock.now += COALESCE_SECONDS
    dispatcher.flush_due()

    assert len(transport.posted) == 2
    assert json.loads(transport.posted[1].payload)["body"] == "2 updates for you in Renamed Studio."
    by_id = {entry.send_id: entry for entry in dispatcher.log.entries()}
    assert by_id[first.send_id].status is SendStatus.SENT
    assert (by_id[second.send_id].status, by_id[second.send_id].carried_by) == (SendStatus.MERGED, third.send_id)
    assert by_id[third.send_id].status is SendStatus.SENT


def test_two_chats_do_not_wait_on_each_other(tmp_path: Path) -> None:
    dispatcher, _clock, transport = _dispatcher(tmp_path)

    dispatcher.submit(_send("one"), "Studio")
    dispatcher.submit(_send("two", chat_id="agent-other"), "Studio")
    dispatcher.flush_due()

    assert len(transport.posted) == 2


def test_a_chat_past_its_limit_is_dropped_and_logged(tmp_path: Path) -> None:
    dispatcher, clock, transport = _dispatcher(tmp_path)
    for _ in range(PER_CHAT_POST_LIMIT):
        assert dispatcher.submit(_send("again"), "Studio").status is SendStatus.QUEUED
        dispatcher.flush_due()
        clock.now += COALESCE_SECONDS

    refused = dispatcher.submit(_send("one more"), "Studio")

    assert refused.status is SendStatus.LIMITED
    assert "not sent" in refused.detail
    assert len(transport.posted) == PER_CHAT_POST_LIMIT
    assert dispatcher.log.entries()[-1].status is SendStatus.LIMITED
    clock.now += PER_CHAT_WINDOW_SECONDS
    assert dispatcher.submit(_send("later"), "Studio").status is SendStatus.QUEUED


def test_the_workspace_as_a_whole_is_limited_too(tmp_path: Path) -> None:
    dispatcher, _clock, _transport = _dispatcher(tmp_path)
    receipts = [dispatcher.submit(_send("hi", chat_id=f"agent-c{index}"), "Studio") for index in range(30)]

    statuses = [receipt.status for receipt in receipts]
    assert statuses.count(SendStatus.QUEUED) == WORKSPACE_POST_LIMIT
    assert statuses[WORKSPACE_POST_LIMIT:] == [SendStatus.LIMITED] * (30 - WORKSPACE_POST_LIMIT)


def test_a_post_waiting_out_its_window_does_not_keep_earlier_ones_counted(tmp_path: Path) -> None:
    dispatcher, clock, _transport = _dispatcher(tmp_path)
    dispatcher.submit(_send("one"), "Studio")
    dispatcher.flush_due()
    clock.now += 5
    # Counted at the end of the chat's coalescing window, after the posts other chats make now.
    dispatcher.submit(_send("two"), "Studio")
    clock.now += 1
    for index in range(WORKSPACE_POST_LIMIT - 2):
        dispatcher.submit(_send("hi", chat_id=f"agent-c{index}"), "Studio")

    clock.now += WORKSPACE_WINDOW_SECONDS
    receipts = [
        dispatcher.submit(_send("hi", chat_id=f"agent-d{index}"), "Studio")
        for index in range(WORKSPACE_POST_LIMIT - 1)
    ]

    assert [receipt.status for receipt in receipts] == [SendStatus.QUEUED] * (WORKSPACE_POST_LIMIT - 1)


def test_a_send_with_no_device_is_logged_and_posts_nothing(tmp_path: Path) -> None:
    dispatcher, _clock, transport = _dispatcher(tmp_path, is_device_registered=False)

    receipt = dispatcher.submit(_send("one"), "Studio")
    dispatcher.flush_due()

    assert receipt.status is SendStatus.NO_DEVICES
    assert transport.posted == []
    assert [entry.status for entry in dispatcher.log.entries()] == [SendStatus.NO_DEVICES]


def test_a_queued_send_whose_devices_went_away_before_it_posted_is_logged_as_reaching_none(tmp_path: Path) -> None:
    dispatcher, clock, transport = _dispatcher(tmp_path)
    dispatcher.submit(_send("one"), "Studio")
    dispatcher.flush_due()
    queued = dispatcher.submit(_send("two"), "Studio")

    dispatcher.notifier.forget(frozenset(device.device_id for device in dispatcher.notifier.list_devices()))
    clock.now += COALESCE_SECONDS
    dispatcher.flush_due()

    assert len(transport.posted) == 1
    (entry,) = [entry for entry in dispatcher.log.entries() if entry.send_id == queued.send_id]
    assert entry.status is SendStatus.NO_DEVICES
    assert entry.shown_title is None


def test_forgetting_pending_sends_posts_none_of_them(tmp_path: Path) -> None:
    dispatcher, clock, transport = _dispatcher(tmp_path)
    dispatcher.submit(_send("one"), "Studio")
    dispatcher.flush_due()
    dropped = dispatcher.submit(_send("two"), "Studio")

    dispatcher.forget_pending()
    clock.now += COALESCE_SECONDS
    dispatcher.flush_due()

    assert len(transport.posted) == 1
    assert dispatcher.next_due_at() is None
    assert [entry.status for entry in dispatcher.log.entries() if entry.send_id == dropped.send_id] == [
        SendStatus.CANCELLED
    ]


def test_the_log_keeps_only_the_latest_sends(tmp_path: Path) -> None:
    dispatcher, _clock, _transport = _dispatcher(tmp_path, is_device_registered=False)

    for index in range(MAX_SEND_LOG_ENTRIES + 5):
        dispatcher.submit(_send(f"message {index}"), "Studio")

    entries = dispatcher.log.entries()
    assert len(entries) == MAX_SEND_LOG_ENTRIES
    assert entries[-1].requested_text == f"message {MAX_SEND_LOG_ENTRIES + 4}"


class _SignallingTransport(RecordingPushTransport):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    posted_event: threading.Event = Field(default_factory=threading.Event, description="Set on the first post")

    def post(self, subscription: BrowserPushSubscription, payload: str, signing_key: Vapid) -> PushProviderAnswer:
        answer = super().post(subscription, payload, signing_key)
        self.posted_event.set()
        return answer


class _FailingOnceTransport(RecordingPushTransport):
    """Fails the first post as an unwritable state file would, and records the rest."""

    has_failed: bool = Field(default=False, description="Whether the first post has failed yet")

    def post(self, subscription: BrowserPushSubscription, payload: str, signing_key: Vapid) -> PushProviderAnswer:
        if not self.has_failed:
            self.has_failed = True
            raise ShellStateError("cannot write shell state file")
        return super().post(subscription, payload, signing_key)


def test_a_failed_batch_does_not_hold_back_another_chats(tmp_path: Path) -> None:
    dispatcher, _, transport = _dispatcher(tmp_path, transport=_FailingOnceTransport())
    dispatcher.submit(_send("one"), "Studio")
    second = dispatcher.submit(_send("two", chat_id="agent-other"), "Studio")

    dispatcher.flush_due()

    assert len(transport.posted) == 1
    by_id = {entry.send_id: entry for entry in dispatcher.log.entries()}
    assert by_id[second.send_id].status is SendStatus.SENT


def test_the_worker_thread_posts_without_anyone_flushing(tmp_path: Path) -> None:
    transport = _SignallingTransport()
    dispatcher, _, _ = _dispatcher(tmp_path, transport=transport)
    dispatcher.start()
    try:
        dispatcher.submit(_send("one"), "Studio")
        assert transport.posted_event.wait(10)
    finally:
        dispatcher.stop()

    assert len(transport.posted) == 1
