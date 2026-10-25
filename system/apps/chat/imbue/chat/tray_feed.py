"""What this app tells the shell for the taskbar's tray (the multiplayer spec, sections 6.4 and 6.5): a speech
bubble for every message a person or a visiting agent sends and for every turn the workspace agent closes with
text, and what every chat is doing whenever the chat list changes.

Both are best-effort loopback posts made off the caller's thread, one at a time: a send and a chat-list push never
wait on the shell. The chats' activity is coalesced, since the list changes far more often than a tray redraws:
a report queued behind one not yet posted replaces it. The last report is posted again once a minute: the shell
keeps it in memory alone, so one restarted since the last chat-list change paints the agent's card from the re-post.
"""

import threading
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from workspace_layout.feed import BubblePost
from workspace_layout.feed import ChatActivity
from workspace_layout.feed import ChatsActivityReport
from workspace_layout.interfaces import ShellLayoutInterface
from workspace_layout.primitives import ChatActivityKind
from workspace_layout.primitives import ChatActivityStatus
from workspace_layout.primitives import ParticipantKey

from imbue.chat.models import ChatSnapshot
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.chat.primitives import ChatStatus
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# How much of a message a bubble shows (the multiplayer spec, section 6.5).
BUBBLE_TEXT_LIMIT: Final[int] = 200
# How often the last chats-activity report is posted again: the shell keeps it in memory, so one restarted since
# the last chat-list change paints the agent's card from this.
CHATS_ACTIVITY_REPOST_INTERVAL_SECONDS: Final[float] = 60.0


@pure
def bubble_text(text: str) -> str:
    """What a bubble shows of a message: its first ``BUBBLE_TEXT_LIMIT`` characters, the ends trimmed."""
    return text.strip()[:BUBBLE_TEXT_LIMIT]


@pure
def first_paragraph(text: str) -> str:
    """The first paragraph of an agent's turn (split on blank lines), which is what its bubble shows."""
    for paragraph in text.strip().split("\n\n"):
        if paragraph.strip():
            return paragraph.strip()
    return ""


def _settled_future() -> Future[None]:
    """A future that has already settled: what a post nothing waits on answers."""
    settled: Future[None] = Future()
    settled.set_result(None)
    return settled


def _log_failed_post(described: str, future: Future[None]) -> None:
    """Log what ended a post the shell client did not handle itself (it answers an unreachable or refusing shell
    with a log of its own); nothing else reads the future."""
    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        logger.opt(exception=error).warning("Posting {} to the shell's tray failed", described)


@pure
def activity_status_of(status: ChatStatus) -> ChatActivityStatus:
    """A chat's status as the shell's wire enum spells it, one for one."""
    match status:
        case ChatStatus.WORKING:
            return ChatActivityStatus.WORKING
        case ChatStatus.BACKGROUND:
            return ChatActivityStatus.BACKGROUND
        case ChatStatus.IDLE:
            return ChatActivityStatus.IDLE
        case ChatStatus.ATTENTION:
            return ChatActivityStatus.ATTENTION
        case ChatStatus.STOPPED:
            return ChatActivityStatus.STOPPED
        case ChatStatus.ERROR:
            return ChatActivityStatus.ERROR
        case _ as unreachable:
            assert_never(unreachable)


@pure
def activity_kind_of(kind: ChatKind) -> ChatActivityKind:
    """A chat's kind as the shell's wire enum spells it, one for one."""
    match kind:
        case ChatKind.AGENT:
            return ChatActivityKind.AGENT
        case ChatKind.ROOM:
            return ChatActivityKind.ROOM
        case _ as unreachable:
            assert_never(unreachable)


@pure
def chats_activity_report(snapshots: Sequence[ChatSnapshot]) -> ChatsActivityReport:
    """Every chat as the tray's agent card lists it: its title, what it is doing, and what it is."""
    return ChatsActivityReport(
        chats=tuple(
            ChatActivity(
                chat_id=str(snapshot.chat_id),
                title=snapshot.title,
                status=activity_status_of(snapshot.status),
                kind=activity_kind_of(snapshot.kind),
            )
            for snapshot in snapshots
        )
    )


class TrayFeed(MutableModel):
    """Posts the tray's feed to the shell, one post at a time, off the caller's thread."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    shell: ShellLayoutInterface = Field(frozen=True, description="The shell the feed goes to")
    repost_interval_seconds: float = Field(
        default=CHATS_ACTIVITY_REPOST_INTERVAL_SECONDS,
        frozen=True,
        description="How often the last chats-activity report is posted again, for a shell restarted since",
    )

    _executor: ThreadPoolExecutor = PrivateAttr(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="tray-feed")
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pending_report: ChatsActivityReport | None = PrivateAttr(default=None)
    _last_report: ChatsActivityReport | None = PrivateAttr(default=None)
    _is_shut_down: bool = PrivateAttr(default=False)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def post_bubble(
        self,
        speaker: ParticipantKey,
        chat_id: ChatId,
        chat_title: str,
        text: str,
        message_id: str,
        mentions: Sequence[ParticipantKey],
    ) -> Future[None]:
        """Queue the bubble a message raises; the future settles once it has been posted, or dropped."""
        bubble = BubblePost(
            speaker=speaker,
            chat_id=str(chat_id),
            chat_title=chat_title,
            text=bubble_text(text),
            message_id=message_id,
            mentions=tuple(mentions),
        )
        return self._submit("a bubble", self.shell.post_bubble, bubble)

    def report_chats(self, snapshots: Sequence[ChatSnapshot]) -> Future[None]:
        """Queue what every chat is doing; a report queued behind one not yet posted replaces it."""
        report = chats_activity_report(snapshots)
        return self._queue_report(report)

    def _queue_report(self, report: ChatsActivityReport) -> Future[None]:
        """Queue one report; a report queued behind one not yet posted replaces it."""
        with self._lock:
            is_post_queued = self._pending_report is not None
            self._pending_report = report
        if is_post_queued:
            return _settled_future()
        return self._submit("the chats' activity", self._post_pending_report)

    def start(self) -> None:
        """Post the last chats-activity report again every ``repost_interval_seconds``, for a shell restarted since
        the last chat-list change (it keeps the report in memory alone)."""
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._repost, daemon=True, name="tray-feed-repost")
        self._thread = thread
        thread.start()

    def report_chats_again(self) -> None:
        """Queue the last report posted once more, for a shell restarted since; nothing when none was posted yet, or
        while a report is queued already (that one is newer, and a re-post never replaces it)."""
        with self._lock:
            if self._last_report is None or self._pending_report is not None:
                return
            self._pending_report = self._last_report
        self._submit("the chats' activity", self._post_pending_report)

    def _repost(self) -> None:
        while not self._stop.wait(timeout=self.repost_interval_seconds):
            self.report_chats_again()

    def shutdown(self) -> None:
        """Drop the posts not yet started, and every later one, and let the one in flight finish on its own.
        Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            self._is_shut_down = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(self, described: str, post: Callable[..., None], *args: object) -> Future[None]:
        """Queue one post, logged as ``described`` should it fail. A feed that is shut down drops it and answers a
        settled future: the manager's threads still push the chat list while they wind down, and a post is
        best-effort, never an error."""
        with self._lock:
            if self._is_shut_down:
                return _settled_future()
            future = self._executor.submit(post, *args)
        future.add_done_callback(lambda done: _log_failed_post(described, done))
        return future

    def _post_pending_report(self) -> None:
        with self._lock:
            report = self._pending_report
            self._pending_report = None
            if report is not None:
                self._last_report = report
        if report is not None:
            self.shell.report_chats_activity(report)
