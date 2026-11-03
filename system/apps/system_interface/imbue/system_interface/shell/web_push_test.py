import json
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from py_vapid import Vapid
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.system_interface.shell.web_push import BrowserPushSubscription
from imbue.system_interface.shell.web_push import DEVICES_FILENAME
from imbue.system_interface.shell.web_push import DeviceRegistrationRequest
from imbue.system_interface.shell.web_push import MAX_PUSH_PAYLOAD_BYTES
from imbue.system_interface.shell.web_push import MAX_REGISTERED_DEVICES
from imbue.system_interface.shell.web_push import PushNotificationRequest
from imbue.system_interface.shell.web_push import PushProviderAnswer
from imbue.system_interface.shell.web_push import PushTransportInterface
from imbue.system_interface.shell.web_push import WebPushNotifier
from imbue.system_interface.shell.web_push import device_id_of
from imbue.system_interface.shell.web_push import push_payload
from imbue.system_interface.shell.web_push import push_provider_name
from imbue.system_interface.shell.web_push import with_device_registered
from imbue.system_interface.testing import RecordingPushTransport

_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _registration(endpoint: str, label: str = "Android, Firefox") -> DeviceRegistrationRequest:
    return DeviceRegistrationRequest.model_validate(
        {"subscription": {"endpoint": endpoint, "keys": {"p256dh": "BPublic", "auth": "secret"}}, "label": label}
    )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", "Mozilla"),
        ("https://fcm.googleapis.com/fcm/send/abc", "Google"),
        ("https://web.push.apple.com/QH4", "Apple"),
        ("https://wns2-par02p.notify.windows.com/w/?token=abc", "Microsoft"),
        ("https://push.example.org/abc", "push.example.org"),
    ],
)
def test_push_provider_name_names_the_vendor_or_else_the_host(endpoint: str, expected: str) -> None:
    assert push_provider_name(endpoint) == expected


def test_a_device_registered_past_the_cap_forgets_the_one_registered_longest_ago() -> None:
    devices = ()
    for index in range(MAX_REGISTERED_DEVICES + 1):
        devices = with_device_registered(
            devices, _registration(f"https://push.example.org/{index}"), _NOW + timedelta(minutes=index)
        )

    assert len(devices) == MAX_REGISTERED_DEVICES
    assert devices[0].device_id == device_id_of("https://push.example.org/1")
    assert devices[-1].device_id == device_id_of(f"https://push.example.org/{MAX_REGISTERED_DEVICES}")


def test_registering_a_device_again_moves_it_last_with_its_new_label_and_time() -> None:
    devices = with_device_registered((), _registration("https://push.example.org/a"), _NOW)
    devices = with_device_registered(devices, _registration("https://push.example.org/b"), _NOW)

    devices = with_device_registered(
        devices, _registration("https://push.example.org/a", "iPhone"), _NOW + timedelta(1)
    )

    assert [(device.label, device.registered_at) for device in devices] == [
        ("Android, Firefox", _NOW),
        ("iPhone", _NOW + timedelta(1)),
    ]


def test_devices_and_the_key_survive_a_new_notifier_over_the_same_directory(tmp_path: Path) -> None:
    first = WebPushNotifier(directory=tmp_path, transport=RecordingPushTransport())
    first.register(_registration("https://push.example.org/a"), _NOW)
    public_key = first.public_key()

    second = WebPushNotifier(directory=tmp_path, transport=RecordingPushTransport())

    assert [device.device_id for device in second.list_devices()] == [device_id_of("https://push.example.org/a")]
    assert second.public_key() == public_key


def test_an_unreadable_devices_file_reads_as_no_devices(tmp_path: Path) -> None:
    (tmp_path / DEVICES_FILENAME).write_text("not json")

    notifier = WebPushNotifier(directory=tmp_path, transport=RecordingPushTransport())

    assert notifier.list_devices() == ()


def test_a_device_whose_push_service_did_not_answer_is_kept(tmp_path: Path) -> None:
    transport = RecordingPushTransport(status_code_by_endpoint={"https://push.example.org/a": None})
    notifier = WebPushNotifier(directory=tmp_path, transport=transport)
    notifier.register(_registration("https://push.example.org/a"), _NOW)

    outcome = notifier.send(PushNotificationRequest(title="Done"), None)

    ((delivery,),) = [outcome.deliveries]
    assert (delivery.is_accepted, delivery.is_forgotten, delivery.detail) == (False, False, "unreachable")
    assert len(notifier.list_devices()) == 1


class _MeetingPushTransport(PushTransportInterface):
    """Answers a post only once every expected post is in flight, so a send that posts one after another fails."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    barrier: threading.Barrier = Field(description="Passed once every expected post has arrived")

    def post(self, subscription: BrowserPushSubscription, payload: str, signing_key: Vapid) -> PushProviderAnswer:
        self.barrier.wait()
        return PushProviderAnswer(status_code=201, detail="")


def test_every_device_is_posted_to_at_once(tmp_path: Path) -> None:
    transport = _MeetingPushTransport(barrier=threading.Barrier(3, timeout=10))
    notifier = WebPushNotifier(directory=tmp_path, transport=transport)
    for index in range(3):
        notifier.register(_registration(f"https://push.example.org/{index}"), _NOW)

    outcome = notifier.send(PushNotificationRequest(title="Done"), None)

    assert outcome.accepted_count == 3
    assert [delivery.device_id for delivery in outcome.deliveries] == [
        device_id_of(f"https://push.example.org/{index}") for index in range(3)
    ]


def test_a_non_ascii_message_travels_as_utf8_within_the_push_services_limit() -> None:
    body = "Отчёт готов, можно смотреть. " * 34

    payload = push_payload(PushNotificationRequest(title="Отчёт готов", body=body[:1000]))

    assert "Отчёт готов" in payload
    assert json.loads(payload)["body"] == body[:1000]
    assert len(payload.encode("utf-8")) <= MAX_PUSH_PAYLOAD_BYTES


def test_a_notification_too_large_for_one_push_message_is_refused() -> None:
    # 1000 four-byte characters: within the body's bound in characters, over the push services' in bytes.
    with pytest.raises(ValidationError, match="bytes as sent"):
        PushNotificationRequest(title="Done", body="\U0001f600" * 1000)


def test_a_notification_title_is_required_and_bounded() -> None:
    with pytest.raises(ValidationError):
        PushNotificationRequest(title="")
    with pytest.raises(ValidationError):
        PushNotificationRequest(title="x" * 121)
