"""What stands between a chat's agent and the owner's devices: the send route's request, the limits that keep a
chatty agent or a routine from flooding a phone, the coalescing that folds a burst from one chat into one
notification, the worker thread that posts off the request thread, and the send log the notifications page shows.

A chat's first notification goes out at once. Another from the same chat within ``COALESCE_SECONDS`` of the last one
posted waits for that window to end and goes out with every other one that arrived meanwhile, as one notification
("3 updates") that replaces the earlier one on the device. Each post counts against two limits, one per chat and one
for the whole workspace; a send that would pass either is dropped, logged, and answered 429.

The notification names the chat it came from as its title. Its text is the agent's own words only when the owner
turned full text on; otherwise it names the workspace and nothing the agent said, since a phone shows it on its
locked screen."""

import secrets
import threading
import time
from bisect import insort
from collections import deque
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

from app_manifest.primitives import AppName
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import field_validator
from pydantic import model_validator

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import ShellError
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic
from imbue.system_interface.shell.web_push import DeviceDelivery
from imbue.system_interface.shell.web_push import MAX_NOTIFICATION_PATH_LENGTH
from imbue.system_interface.shell.web_push import NotificationSettings
from imbue.system_interface.shell.web_push import PushNotification
from imbue.system_interface.shell.web_push import WebPushNotifier

SEND_LOG_FILENAME: Final[str] = "send_log.json"
SEND_LOG_FILE_VERSION: Final[int] = 1
# How many sends the log keeps, newest last.
MAX_SEND_LOG_ENTRIES: Final[int] = 100

# A later notification from a chat waits until this long after the chat's last post, and goes out with every other
# one that arrived meanwhile.
COALESCE_SECONDS: Final[float] = 60.0
# At most this many posts per chat in a window this long...
PER_CHAT_POST_LIMIT: Final[int] = 4
PER_CHAT_WINDOW_SECONDS: Final[float] = 10 * 60.0
# ...and this many for the whole workspace in a window this long.
WORKSPACE_POST_LIMIT: Final[int] = 20
WORKSPACE_WINDOW_SECONDS: Final[float] = 60 * 60.0

# The longest text a notification shows in full-text mode: a lock screen shows a few lines at most.
FULL_TEXT_MAX_LENGTH: Final[int] = 200
_CHAT_TITLE_MAX_LENGTH: Final[int] = 80
_CHAT_ID_PATTERN: Final[str] = r"^agent-[A-Za-z0-9_-]{1,120}$"
_SEND_ID_BYTES: Final[int] = 8


class PushCaller(LowerCaseStrEnum):
    """Who asked for a send."""

    # A chat's agent, through notify-user and the send token.
    CHAT = auto()
    # The owner, from the notifications page's test button.
    TEST = auto()


class SendStatus(LowerCaseStrEnum):
    """Where a send stands (a wire value of the send log and the send route)."""

    # Accepted, waiting for its chat's coalescing window to end.
    QUEUED = auto()
    # Posted to the devices; the deliveries say what each push service answered.
    SENT = auto()
    # Folded into a later send from the same chat, which carried it.
    MERGED = auto()
    # Dropped: posting it would have passed the chat's or the workspace's limit.
    LIMITED = auto()
    # Dropped: no device has notifications on.
    NO_DEVICES = auto()
    # Dropped: the owner reset notifications while it was queued.
    CANCELLED = auto()


class PushSendRequest(FrozenModel):
    """What ``notify-user`` posts to the send route for one chat's notification."""

    chat_id: str = Field(pattern=_CHAT_ID_PATTERN, description="The chat the notification is from")
    chat_title: str = Field(min_length=1, max_length=200, description="The chat's title, which the notification shows")
    title: str | None = Field(default=None, max_length=200, description="The agent's optional heading")
    message: str = Field(min_length=1, max_length=4000, description="What the agent said")
    app: AppName = Field(description="The app the chat runs in, whose page a tap opens")
    app_path: str = Field(
        min_length=1, max_length=1000, description="The path under the app's origin that shows the chat"
    )

    @field_validator("app_path")
    @classmethod
    def _app_path_is_a_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or "\\" in value:
            raise InvalidShellValueError("the app path is a path under the app's origin, starting with a single '/'")
        return value

    @model_validator(mode="after")
    def _deep_link_fits_a_notification(self) -> "PushSendRequest":
        length = len(deep_link_path(self.app, self.app_path))
        if length > MAX_NOTIFICATION_PATH_LENGTH:
            raise InvalidShellValueError(
                f"the app path makes a {length}-character link; a notification opens at most "
                f"{MAX_NOTIFICATION_PATH_LENGTH}"
            )
        return self


class PushSendReceipt(FrozenModel):
    """What the send route answers: the send's id in the log and where it stands."""

    send_id: str = Field(description="The send's id in the send log")
    status: SendStatus = Field(description="Where the send stands")
    detail: str = Field(description="What that means, for the agent to report")


class SendLogEntry(FrozenModel):
    """One send as the notifications page lists it."""

    send_id: str = Field(description="The send's id")
    requested_at: datetime = Field(description="When it was asked for")
    caller: PushCaller = Field(description="Who asked for it")
    chat_id: str | None = Field(description="The chat it came from; None for a test")
    chat_title: str = Field(description="What the notification named as its sender")
    requested_text: str = Field(description="What the caller asked to show, in full")
    shown_title: str | None = Field(description="The title the devices showed; None until it is posted")
    shown_body: str | None = Field(description="The text the devices showed; None until it is posted")
    status: SendStatus = Field(description="Where it stands")
    carried_by: str | None = Field(description="For a merged send, the send that carried it")
    deliveries: tuple[DeviceDelivery, ...] = Field(description="What each device's push service answered")


class SendLogListing(FrozenModel):
    """The send log, newest first."""

    sends: tuple[SendLogEntry, ...] = Field(description="The latest sends, newest first")


class _SendLogDocument(FrozenModel):
    version: int = Field(description="The file format version")
    sends: tuple[SendLogEntry, ...] = Field(description="The latest sends, oldest first")


class SendLog(MutableModel):
    """``send_log.json``: the latest sends and what became of each."""

    path: Path = Field(frozen=True, description="The log file")

    def entries(self) -> tuple[SendLogEntry, ...]:
        with STATE_FILES_LOCK:
            raw = read_json_object(self.path)
        document = parse_versioned_document(raw, _SendLogDocument, SEND_LOG_FILE_VERSION, self.path)
        return () if document is None else document.sends

    def put(self, entries: tuple[SendLogEntry, ...]) -> None:
        """Add these entries, or replace the ones already logged under their ids, dropping the oldest past the cap."""
        with STATE_FILES_LOCK:
            replacing = {entry.send_id: entry for entry in entries}
            kept = [replacing.pop(entry.send_id, entry) for entry in self.entries()]
            sends = (*kept, *replacing.values())[-MAX_SEND_LOG_ENTRIES:]
            document = _SendLogDocument(version=SEND_LOG_FILE_VERSION, sends=sends)
            write_json_atomic(self.path, document.model_dump(mode="json"))


@pure
def deep_link_path(app_name: str, path: str) -> str:
    """The shell's deep link that opens ``path`` of ``app_name`` (desktop-interface contracts.md section 9)."""
    return "/?" + urlencode({"open": f"{app_name}:{path}"})


@pure
def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


@pure
def requested_text_of(request: PushSendRequest) -> str:
    """What the agent asked to show, as one line of text: its heading, then its message."""
    title = (request.title or "").strip()
    message = request.message.strip()
    return f"{title}: {message}" if title else message


@pure
def compose_notification(
    chat_id: str,
    chat_title: str,
    path: str,
    texts: tuple[str, ...],
    workspace_name: str,
    settings: NotificationSettings,
) -> PushNotification:
    """The notification for one or more sends from one chat: titled with the chat, opening ``path`` (the shell's
    deep link to the chat) on a tap, saying what the agent said only in full-text mode, and replacing the chat's
    earlier notification on the device."""
    count = len(texts)
    if settings.is_full_text:
        latest = texts[-1]
        body = latest if count == 1 else f"{count} updates. Latest: {latest}"
    else:
        body = (
            f"Has something for you in {workspace_name}."
            if count == 1
            else f"{count} updates for you in {workspace_name}."
        )
    return PushNotification(
        title=_cut(chat_title, _CHAT_TITLE_MAX_LENGTH),
        body=_cut(body, FULL_TEXT_MAX_LENGTH),
        path=path,
        tag=f"chat-{chat_id}",
    )


def mint_send_id() -> str:
    return f"send-{secrets.token_hex(_SEND_ID_BYTES)}"


class _PendingBatch(FrozenModel):
    """The sends from one chat waiting for its coalescing window to end."""

    chat_id: str = Field(description="The chat")
    chat_title: str = Field(description="The chat's title, as its latest send gave it")
    path: str = Field(description="The shell's deep link a tap opens, as the latest send gave it")
    workspace_name: str = Field(description="The workspace's name when the latest send arrived")
    send_ids: tuple[str, ...] = Field(description="The sends, oldest first")
    texts: tuple[str, ...] = Field(description="What each send asked to show, oldest first")
    due_at: float = Field(description="When the batch goes out, in clock seconds")


def _utc(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


class PushDispatcher(MutableModel):
    """Accepts chats' sends, applies the limits and the coalescing, and posts each batch on its own thread."""

    notifier: WebPushNotifier = Field(frozen=True, description="The key, the devices, and the posting")
    log: SendLog = Field(frozen=True, description="The send log")
    clock: Callable[[], float] = Field(default=time.time, frozen=True, description="Seconds since the epoch")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pending_by_chat: dict[str, _PendingBatch] = PrivateAttr(default_factory=dict)
    _last_post_at_by_chat: dict[str, float] = PrivateAttr(default_factory=dict)
    _post_times_by_chat: dict[str, deque[float]] = PrivateAttr(default_factory=dict)
    _workspace_post_times: deque[float] = PrivateAttr(default_factory=deque)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _wake: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        self.notifier.send_token()
        thread = threading.Thread(target=self._run, daemon=True, name="shell-web-push")
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def submit(self, request: PushSendRequest, workspace_name: str) -> PushSendReceipt:
        """Accept one chat's send: queue it (alone, or with the chat's pending batch), or drop it when no device has
        notifications on or a limit would be passed; logged either way."""
        now = self.clock()
        send_id = mint_send_id()
        text = requested_text_of(request)
        entry = SendLogEntry(
            send_id=send_id,
            requested_at=_utc(now),
            caller=PushCaller.CHAT,
            chat_id=request.chat_id,
            chat_title=request.chat_title,
            requested_text=text,
            shown_title=None,
            shown_body=None,
            status=SendStatus.QUEUED,
            carried_by=None,
            deliveries=(),
        )
        if not any(not device.is_stale for device in self.notifier.list_devices()):
            self.log.put((entry.model_copy_update(to_update(entry.field_ref().status, SendStatus.NO_DEVICES)),))
            return PushSendReceipt(
                send_id=send_id, status=SendStatus.NO_DEVICES, detail="No device has notifications on."
            )
        with self._lock:
            pending = self._pending_by_chat.get(request.chat_id)
            if pending is not None:
                self._pending_by_chat[request.chat_id] = pending.model_copy_update(
                    to_update(pending.field_ref().chat_title, request.chat_title),
                    to_update(pending.field_ref().path, deep_link_path(request.app, request.app_path)),
                    to_update(pending.field_ref().workspace_name, workspace_name),
                    to_update(pending.field_ref().send_ids, (*pending.send_ids, send_id)),
                    to_update(pending.field_ref().texts, (*pending.texts, text)),
                )
                receipt = PushSendReceipt(
                    send_id=send_id,
                    status=SendStatus.QUEUED,
                    detail="Queued with this chat's other recent notifications, to go out as one.",
                )
            else:
                limit_reason = self._limit_reason_locked(request.chat_id, now)
                if limit_reason is None:
                    last_post_at = self._last_post_at_by_chat.get(request.chat_id)
                    due_at = now if last_post_at is None else max(now, last_post_at + COALESCE_SECONDS)
                    self._pending_by_chat[request.chat_id] = _PendingBatch(
                        chat_id=request.chat_id,
                        chat_title=request.chat_title,
                        path=deep_link_path(request.app, request.app_path),
                        workspace_name=workspace_name,
                        send_ids=(send_id,),
                        texts=(text,),
                        due_at=due_at,
                    )
                    self._count_post_locked(request.chat_id, due_at)
                    receipt = PushSendReceipt(send_id=send_id, status=SendStatus.QUEUED, detail="Queued.")
                else:
                    receipt = PushSendReceipt(send_id=send_id, status=SendStatus.LIMITED, detail=limit_reason)
            # Logged before the lock is let go, so the worker never posts a batch whose send is not yet in the log.
            self.log.put((entry.model_copy_update(to_update(entry.field_ref().status, receipt.status)),))
        if receipt.status is SendStatus.LIMITED:
            logger.warning("Dropped a notification from {}: {}", request.chat_id, receipt.detail)
        self._wake.set()
        return receipt

    def flush_due(self) -> None:
        """Post every batch whose window has ended."""
        now = self.clock()
        with self._lock:
            due = [batch for batch in self._pending_by_chat.values() if batch.due_at <= now]
            for batch in due:
                del self._pending_by_chat[batch.chat_id]
                self._last_post_at_by_chat[batch.chat_id] = now
        for batch in due:
            try:
                self._post(batch)
            except (ShellError, OSError) as e:
                # A state file it could not read or write fails that batch alone: the other chats' still go out.
                logger.opt(exception=e).error("A Web Push batch from {} failed", batch.chat_id)

    def next_due_at(self) -> float | None:
        with self._lock:
            return min((batch.due_at for batch in self._pending_by_chat.values()), default=None)

    def forget_pending(self) -> None:
        """Drop every queued batch (a reset), logging its sends as cancelled: nothing queued for the old devices goes
        out to new ones."""
        with self._lock:
            dropped_ids = {send_id for batch in self._pending_by_chat.values() for send_id in batch.send_ids}
            self._pending_by_chat.clear()
        cancelled = tuple(
            entry.model_copy_update(to_update(entry.field_ref().status, SendStatus.CANCELLED))
            for entry in self.log.entries()
            if entry.send_id in dropped_ids
        )
        if cancelled:
            self.log.put(cancelled)

    def _post(self, batch: _PendingBatch) -> None:
        notification = compose_notification(
            batch.chat_id, batch.chat_title, batch.path, batch.texts, batch.workspace_name, self.notifier.settings()
        )
        outcome = self.notifier.send(notification, None, _utc(self.clock()))
        carrier_id = batch.send_ids[-1]
        logged = {entry.send_id: entry for entry in self.log.entries() if entry.send_id in batch.send_ids}
        updated: list[SendLogEntry] = []
        for send_id in batch.send_ids:
            entry = logged.get(send_id)
            if entry is None:
                continue
            fields = entry.field_ref()
            if send_id == carrier_id:
                updated.append(
                    entry.model_copy_update(
                        to_update(fields.status, SendStatus.SENT),
                        to_update(fields.shown_title, notification.title),
                        to_update(fields.shown_body, notification.body),
                        to_update(fields.deliveries, outcome.deliveries),
                    )
                )
            else:
                updated.append(
                    entry.model_copy_update(
                        to_update(fields.status, SendStatus.MERGED), to_update(fields.carried_by, carrier_id)
                    )
                )
        self.log.put(tuple(updated))

    def log_test(self, notification: PushNotification, deliveries: tuple[DeviceDelivery, ...]) -> None:
        """Log a test the owner sent from the notifications page."""
        self.log.put(
            (
                SendLogEntry(
                    send_id=mint_send_id(),
                    requested_at=_utc(self.clock()),
                    caller=PushCaller.TEST,
                    chat_id=None,
                    chat_title=notification.title,
                    requested_text=notification.body,
                    shown_title=notification.title,
                    shown_body=notification.body,
                    status=SendStatus.SENT,
                    carried_by=None,
                    deliveries=deliveries,
                ),
            )
        )

    def _limit_reason_locked(self, chat_id: str, now: float) -> str | None:
        chat_times = self._post_times_by_chat.setdefault(chat_id, deque())
        _drop_older_than(chat_times, now - PER_CHAT_WINDOW_SECONDS)
        _drop_older_than(self._workspace_post_times, now - WORKSPACE_WINDOW_SECONDS)
        if len(chat_times) >= PER_CHAT_POST_LIMIT:
            return (
                f"This chat already sent {PER_CHAT_POST_LIMIT} notifications in the last "
                f"{int(PER_CHAT_WINDOW_SECONDS // 60)} minutes; this one was not sent."
            )
        if len(self._workspace_post_times) >= WORKSPACE_POST_LIMIT:
            return (
                f"The workspace already sent {WORKSPACE_POST_LIMIT} notifications in the last "
                f"{int(WORKSPACE_WINDOW_SECONDS // 60)} minutes; this one was not sent."
            )
        return None

    def _count_post_locked(self, chat_id: str, at: float) -> None:
        # In order, as _drop_older_than needs: a chat's post can be due after one another chat posts later.
        insort(self._post_times_by_chat.setdefault(chat_id, deque()), at)
        insort(self._workspace_post_times, at)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.flush_due()
            next_due_at = self.next_due_at()
            timeout = None if next_due_at is None else max(0.0, next_due_at - self.clock())
            self._wake.wait(timeout)
            self._wake.clear()


def _drop_older_than(times: deque[float], cutoff: float) -> None:
    while times and times[0] <= cutoff:
        times.popleft()
