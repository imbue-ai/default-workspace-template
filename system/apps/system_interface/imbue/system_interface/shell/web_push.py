"""Web Push (RFC 8030, 8291, 8292) for the devices that turned notifications on from the shell's notifications page:
the shell's own VAPID signing key, the devices' push subscriptions, and one encrypted post per device to the push
service its browser chose (Mozilla's, Google's, Apple's).

A subscription belongs to one origin, and the shell's origin is the one a phone saves to its home screen, so the
shell holds them; agents and apps in the workspace reach every device through the shell's loopback send route. Both
files live under ``web_push/`` in the shell's state directory: ``vapid_private_key.pem``, made on first use and
readable by this user alone, and ``devices.json``. A lost key or file means each device turns notifications on again;
nothing else is lost."""

import base64
import hashlib
import json
import os
import re
from abc import ABC
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from loguru import logger
from py_vapid import Vapid
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator
from pywebpush import WebPushException
from pywebpush import webpush

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import ShellError
from imbue.system_interface.shell.primitives import PushDeviceId
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

WEB_PUSH_DIRECTORY_NAME: Final[str] = "web_push"
VAPID_KEY_FILENAME: Final[str] = "vapid_private_key.pem"
DEVICES_FILENAME: Final[str] = "devices.json"
DEVICES_FILE_VERSION: Final[int] = 1

# The contact a push service may use about this sender. Apple's refuses a claim without a real mailto: or https:
# subject; nothing is ever sent to it.
VAPID_SUBJECT: Final[str] = "https://imbue.com"
# How long a push service holds a message for a device that is off or asleep: a day, after which a "your report is
# ready" has been overtaken by the user coming back on their own.
PUSH_TTL_SECONDS: Final[int] = 24 * 60 * 60
# Devices are posted to at once, each waiting this long to connect and this long for each read, so a push service
# that hangs delays the whole send by about this much.
PUSH_POST_TIMEOUT_SECONDS: Final[float] = 5.0
# How many devices the file keeps; registering one more forgets the one registered longest ago.
MAX_REGISTERED_DEVICES: Final[int] = 20
# A push service's answer for a subscription that no longer exists (RFC 8030 section 7.3): the device is forgotten.
_GONE_STATUS_CODES: Final[frozenset[int]] = frozenset({404, 410})
_ACCEPTED_STATUS_CODES: Final[frozenset[int]] = frozenset({200, 201, 202})
# The longest payload one aes128gcm record carries within the push services' 4096-byte message limit (RFC 8291): the
# record's 86-byte header, its padding delimiter and its 16-byte tag take the rest.
MAX_PUSH_PAYLOAD_BYTES: Final[int] = 3993
# How much of a push service's answer a delivery keeps, for the page's raw view and the log.
_ANSWER_EXCERPT_LENGTH: Final[int] = 500
_DEVICE_ID_LENGTH: Final[int] = 16
# A key off this alphabet makes pywebpush raise outside the errors a post reports, failing the send to every device.
_BASE64URL_PATTERN: Final[str] = r"^[A-Za-z0-9_-]+={0,2}$"
# The endpoint's origin is the audience the VAPID claim is signed for, and py_vapid refuses one off this shape, failing
# the send to every device.
_SIGNABLE_ORIGIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"https://[^/:]+(:\d+)?")

_PUSH_PROVIDER_NAME_BY_DOMAIN: Final[dict[str, str]] = {
    "mozilla.com": "Mozilla",
    "googleapis.com": "Google",
    "push.apple.com": "Apple",
    "notify.windows.com": "Microsoft",
}


class UnexpectedPushAnswerError(ShellError):
    """``pywebpush`` answered something other than the push service's response."""


class PushSubscriptionKeys(FrozenModel):
    """The keys a browser hands over with a subscription, which the message is encrypted to (RFC 8291)."""

    p256dh: str = Field(
        min_length=1, max_length=256, pattern=_BASE64URL_PATTERN, description="The device's public key, base64url"
    )
    auth: str = Field(
        min_length=1,
        max_length=64,
        pattern=_BASE64URL_PATTERN,
        description="The device's authentication secret, base64url",
    )


class BrowserPushSubscription(FrozenModel):
    """A ``PushSubscription`` as the browser's ``toJSON()`` writes it."""

    # The browser's own object, which carries fields this build does not read.
    model_config = ConfigDict(extra="ignore")

    endpoint: str = Field(
        min_length=1, max_length=2048, description="The push service URL the browser chose for this device"
    )
    keys: PushSubscriptionKeys = Field(description="The keys the message is encrypted to")

    @field_validator("endpoint")
    @classmethod
    def _endpoint_is_https(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or not _SIGNABLE_ORIGIN_PATTERN.fullmatch(f"https://{parts.netloc}")
        ):
            raise InvalidShellValueError("a push endpoint is an https URL on a host name, with at most a port")
        return value


class DeviceRegistrationRequest(FrozenModel):
    """What the notifications page posts once a device has subscribed."""

    subscription: BrowserPushSubscription = Field(description="The device's push subscription")
    label: str = Field(
        min_length=1, max_length=120, description="What to call the device, from its browser and system"
    )


class RegisteredDevice(FrozenModel):
    """One device that turned notifications on, as ``devices.json`` keeps it."""

    device_id: PushDeviceId = Field(description="The device's id: a digest of its push endpoint")
    label: str = Field(description="What to call the device")
    subscription: BrowserPushSubscription = Field(description="The device's push subscription")
    registered_at: datetime = Field(description="When the device last turned notifications on")


class _DevicesDocument(FrozenModel):
    """The whole of ``devices.json``."""

    version: int = Field(description="The file format version")
    devices: tuple[RegisteredDevice, ...] = Field(description="The registered devices, oldest first")


class DeviceView(FrozenModel):
    """A registered device as the notifications page lists it: its subscription stays on the server."""

    device_id: PushDeviceId = Field(description="The device's id: a digest of its push endpoint")
    label: str = Field(description="What to call the device")
    push_provider: str = Field(description="Whose push service carries the device's messages")
    registered_at: datetime = Field(description="When the device last turned notifications on")


class DevicesListing(FrozenModel):
    """Every device that turned notifications on."""

    devices: tuple[DeviceView, ...] = Field(description="The registered devices, oldest first")


class WebPushPublicKey(FrozenModel):
    """The key a browser subscribes with, so the push service accepts only messages this shell signed."""

    public_key: str = Field(description="The VAPID public key: the raw P-256 point, unpadded base64url")


class PushNotificationRequest(FrozenModel):
    """One notification to show on every registered device."""

    title: str = Field(min_length=1, max_length=120, description="The notification's title")
    body: str = Field(default="", max_length=1000, description="The notification's text")
    path: str = Field(
        default="/",
        max_length=2048,
        description="The page of the workspace a tap opens, relative to the shell's root: '/' for the desktop, or a "
        "deep link such as '/?open=chat:/?chat=<chat-id>'",
    )
    tag: str | None = Field(
        default=None,
        max_length=120,
        description="A notification with the same tag replaces the earlier one on the device instead of adding one",
    )

    @field_validator("path")
    @classmethod
    def _path_is_the_shells(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "\\" in value:
            raise InvalidShellValueError("the path is a path under the shell's root, starting with a single '/'")
        return value

    @model_validator(mode="after")
    def _payload_fits_one_push_message(self) -> "PushNotificationRequest":
        size = len(push_payload(self).encode("utf-8"))
        if size > MAX_PUSH_PAYLOAD_BYTES:
            raise InvalidShellValueError(
                f"the notification is {size} bytes as sent; a push service takes at most {MAX_PUSH_PAYLOAD_BYTES}"
            )
        return self


class PushProviderAnswer(FrozenModel):
    """What one post to a push service came back with."""

    status_code: int | None = Field(description="The HTTP status, or None when there was no answer")
    detail: str = Field(description="The start of the answer's body, or why there was no answer")


class DeviceDelivery(FrozenModel):
    """What happened to one notification on its way to one device."""

    device_id: PushDeviceId = Field(description="The device's id")
    label: str = Field(description="What to call the device")
    push_provider: str = Field(description="Whose push service was posted to")
    status_code: int | None = Field(description="The push service's HTTP status, or None when it did not answer")
    detail: str = Field(description="The start of the push service's answer, or why there was none")
    is_accepted: bool = Field(
        description="Whether the push service took the message; not proof that the device showed it"
    )
    is_forgotten: bool = Field(
        description="Whether the push service said the subscription is gone, so the device was forgotten"
    )


class PushSendOutcome(FrozenModel):
    """What a send did, device by device."""

    deliveries: tuple[DeviceDelivery, ...] = Field(description="One entry per device posted to")
    accepted_count: int = Field(description="How many push services took the message")


@pure
def device_id_of(endpoint: str) -> PushDeviceId:
    """The id a device is listed and addressed under: a digest of its endpoint, which itself stays on the server."""
    return PushDeviceId(hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:_DEVICE_ID_LENGTH])


@pure
def push_provider_name(endpoint: str) -> str:
    """Whose push service an endpoint is, for a reader: a known vendor's name, else the endpoint's host."""
    host = urlsplit(endpoint).hostname or ""
    for domain, name in _PUSH_PROVIDER_NAME_BY_DOMAIN.items():
        if host == domain or host.endswith(f".{domain}"):
            return name
    return host


@pure
def device_view(device: RegisteredDevice) -> DeviceView:
    return DeviceView(
        device_id=device.device_id,
        label=device.label,
        push_provider=push_provider_name(device.subscription.endpoint),
        registered_at=device.registered_at,
    )


@pure
def push_payload(notification: PushNotificationRequest) -> str:
    """The JSON the service worker reads off a push event (``sw.js``), in plain UTF-8: an escaped non-ASCII character
    takes six bytes of the push services' 4 KB message limit."""
    return json.dumps(
        {"title": notification.title, "body": notification.body, "path": notification.path, "tag": notification.tag},
        ensure_ascii=False,
    )


@pure
def with_device_registered(
    devices: tuple[RegisteredDevice, ...], registration: DeviceRegistrationRequest, now: datetime
) -> tuple[RegisteredDevice, ...]:
    """The devices with this one added last, replacing an earlier registration of the same subscription, and the
    longest-registered dropped past the cap."""
    device = RegisteredDevice(
        device_id=device_id_of(registration.subscription.endpoint),
        label=registration.label,
        subscription=registration.subscription,
        registered_at=now,
    )
    kept = tuple(existing for existing in devices if existing.device_id != device.device_id)
    return (*kept, device)[-MAX_REGISTERED_DEVICES:]


class PushTransportInterface(MutableModel, ABC):
    """Posts one encrypted, signed message to one device's push service."""

    @abstractmethod
    def post(self, subscription: BrowserPushSubscription, payload: str, signing_key: Vapid) -> PushProviderAnswer:
        """Encrypt ``payload`` to the subscription's keys, sign the request with ``signing_key``, and post it; never
        raises for a push service that refuses or cannot be reached (the answer says so)."""


class PywebpushTransport(PushTransportInterface):
    """The real transport: ``pywebpush``'s encryption and VAPID signing, posted over HTTPS."""

    def post(self, subscription: BrowserPushSubscription, payload: str, signing_key: Vapid) -> PushProviderAnswer:
        try:
            response = webpush(
                subscription_info=subscription.model_dump(mode="json"),
                data=payload,
                vapid_private_key=signing_key,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=PUSH_TTL_SECONDS,
                # High urgency is what lets a phone that is saving its battery wake for a message (RFC 8030 5.3).
                headers={"Urgency": "high"},
                timeout=PUSH_POST_TIMEOUT_SECONDS,
            )
        except WebPushException as e:
            if e.response is None:
                return PushProviderAnswer(status_code=None, detail=str(e))
            response = e.response
        except (requests.RequestException, ValueError) as e:
            # No answer at all, or keys the browser handed over that do not decode.
            return PushProviderAnswer(status_code=None, detail=f"{type(e).__name__}: {e}")
        # webpush answers a string only when asked for a curl command, which this never does.
        if isinstance(response, str):
            raise UnexpectedPushAnswerError("webpush answered a curl command instead of posting")
        return PushProviderAnswer(status_code=response.status_code, detail=response.text[:_ANSWER_EXCERPT_LENGTH])


class WebPushNotifier(MutableModel):
    """The shell's signing key and its registered devices, and the sends to them."""

    directory: Path = Field(frozen=True, description="Where the key and the devices file live")
    transport: PushTransportInterface = Field(frozen=True, description="How a message reaches a push service")

    def _key_path(self) -> Path:
        return self.directory / VAPID_KEY_FILENAME

    def _devices_path(self) -> Path:
        return self.directory / DEVICES_FILENAME

    def signing_key(self) -> Vapid:
        """The shell's VAPID key, made and kept (readable by this user alone) the first time it is asked for."""
        key_path = self._key_path()
        with STATE_FILES_LOCK:
            if not key_path.is_file():
                key = Vapid()
                key.generate_keys()
                self.directory.mkdir(parents=True, exist_ok=True)
                temp_path = key_path.with_name(f"{key_path.name}.tmp")
                file_descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(file_descriptor, "wb") as key_file:
                    key_file.write(key.private_pem())
                os.replace(temp_path, key_path)
                logger.info("Made the shell's Web Push signing key at {}", key_path)
            return Vapid.from_pem(key_path.read_bytes())

    def public_key(self) -> str:
        """The public half of the signing key as ``PushManager.subscribe`` takes an ``applicationServerKey``: the raw
        P-256 point, unpadded base64url."""
        raw_point = self.signing_key().public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(raw_point).rstrip(b"=").decode("ascii")

    def list_devices(self) -> tuple[RegisteredDevice, ...]:
        with STATE_FILES_LOCK:
            raw = read_json_object(self._devices_path())
        document = parse_versioned_document(raw, _DevicesDocument, DEVICES_FILE_VERSION, self._devices_path())
        return () if document is None else document.devices

    def _write_devices(self, devices: tuple[RegisteredDevice, ...]) -> None:
        document = _DevicesDocument(version=DEVICES_FILE_VERSION, devices=devices)
        write_json_atomic(self._devices_path(), document.model_dump(mode="json"))

    def register(self, registration: DeviceRegistrationRequest, now: datetime) -> RegisteredDevice:
        """Keep a device's subscription, replacing an earlier one of the same subscription."""
        with STATE_FILES_LOCK:
            devices = with_device_registered(self.list_devices(), registration, now)
            self._write_devices(devices)
        return devices[-1]

    def forget(self, device_ids: frozenset[PushDeviceId]) -> bool:
        """Forget these devices; whether any of them was registered."""
        with STATE_FILES_LOCK:
            devices = self.list_devices()
            kept = tuple(device for device in devices if device.device_id not in device_ids)
            if len(kept) == len(devices):
                return False
            self._write_devices(kept)
        return True

    def send(
        self, notification: PushNotificationRequest, device_ids: frozenset[PushDeviceId] | None
    ) -> PushSendOutcome:
        """Post the notification to every registered device (or to those of ``device_ids``), forgetting each one
        whose push service says its subscription is gone."""
        devices = tuple(
            device for device in self.list_devices() if device_ids is None or device.device_id in device_ids
        )
        if not devices:
            return PushSendOutcome(deliveries=(), accepted_count=0)
        signing_key = self.signing_key()
        payload = push_payload(notification)
        with ThreadPoolExecutor(max_workers=len(devices), thread_name_prefix="web-push") as executor:
            answers = tuple(
                executor.map(lambda device: self.transport.post(device.subscription, payload, signing_key), devices)
            )
        deliveries: list[DeviceDelivery] = []
        for device, answer in zip(devices, answers, strict=True):
            delivery = DeviceDelivery(
                device_id=device.device_id,
                label=device.label,
                push_provider=push_provider_name(device.subscription.endpoint),
                status_code=answer.status_code,
                detail=answer.detail,
                is_accepted=answer.status_code in _ACCEPTED_STATUS_CODES,
                is_forgotten=answer.status_code in _GONE_STATUS_CODES,
            )
            if not delivery.is_accepted:
                logger.warning(
                    "Web Push to {} ({}) was not accepted: {} {}",
                    delivery.label,
                    delivery.push_provider,
                    delivery.status_code,
                    delivery.detail,
                )
            deliveries.append(delivery)
        gone = frozenset(delivery.device_id for delivery in deliveries if delivery.is_forgotten)
        if gone:
            self.forget(gone)
        return PushSendOutcome(
            deliveries=tuple(deliveries),
            accepted_count=sum(1 for delivery in deliveries if delivery.is_accepted),
        )
