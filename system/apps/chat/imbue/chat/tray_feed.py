"""What this app tells the shell for the taskbar's tray (the multiplayer spec, sections 6.4 and 6.5): a speech
bubble for every message a person or a visiting agent sends and for every turn the workspace agent closes with
text, and what every chat is doing whenever the chat list changes.

Both are best-effort loopback posts made off the caller's thread, one at a time: a send and a chat-list push never
wait on the shell. The chats' activity is coalesced, since the list changes far more often than a tray redraws:
a report queued behind one not yet posted replaces it.
"""

import threading
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from typing import Final

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
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# How much of a message a bubble shows (the multiplayer spec, section 6.5).
BUBBLE_TEXT_LIMIT: Final[int] = 200


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


@pure
def chats_activity_report(snapshots: Sequence[ChatSnapshot]) -> ChatsActivityReport:
    """Every chat as the tray's agent card lists it: its title, what it is doing, and what it is."""
    return ChatsActivityReport(
        chats=tuple(
            ChatActivity(
                chat_id=str(snapshot.chat_id),
                title=snapshot.title,
                status=ChatActivityStatus(snapshot.status.value),
                kind=ChatActivityKind(snapshot.kind.value),
            )
            for snapshot in snapshots
        )
    )


class TrayFeed(MutableModel):
    """Posts the tray's feed to the shell, one post at a time, off the caller's thread."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    shell: ShellLayoutInterface = Field(frozen=True, description="The shell the feed goes to")

    _executor: ThreadPoolExecutor = PrivateAttr(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="tray-feed")
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _pending_report: ChatsActivityReport | None = PrivateAttr(default=None)

    def post_bubble(
        self,
        speaker: ParticipantKey,
        chat_id: ChatId,
        chat_title: str,
        text: str,
        message_id: str,
        mentions: Sequence[ParticipantKey] = (),
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
        return self._executor.submit(self.shell.post_bubble, bubble)

    def report_chats(self, snapshots: Sequence[ChatSnapshot]) -> Future[None]:
        """Queue what every chat is doing; a report queued behind one not yet posted replaces it."""
        report = chats_activity_report(snapshots)
        with self._lock:
            is_post_queued = self._pending_report is not None
            self._pending_report = report
        if is_post_queued:
            settled: Future[None] = Future()
            settled.set_result(None)
            return settled
        return self._executor.submit(self._post_pending_report)

    def shutdown(self) -> None:
        """Drop the posts not yet started and let the one in flight finish on its own. Idempotent."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _post_pending_report(self) -> None:
        with self._lock:
            report = self._pending_report
            self._pending_report = None
        if report is not None:
            self.shell.report_chats_activity(report)
