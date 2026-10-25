import threading

from pydantic import Field
from workspace_layout.feed import BubblePost
from workspace_layout.feed import ChatsActivityReport
from workspace_layout.primitives import ChatActivityKind
from workspace_layout.primitives import ChatActivityStatus
from workspace_layout.primitives import ParticipantKey
from workspace_layout.testing import FakeShell

from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.chat.primitives import ChatStatus
from imbue.chat.testing import make_chat_snapshot
from imbue.chat.testing import wait_until_true
from imbue.chat.tray_feed import BUBBLE_TEXT_LIMIT
from imbue.chat.tray_feed import TrayFeed
from imbue.chat.tray_feed import activity_kind_of
from imbue.chat.tray_feed import activity_status_of
from imbue.chat.tray_feed import bubble_text
from imbue.chat.tray_feed import chats_activity_report
from imbue.chat.tray_feed import first_paragraph
from imbue.imbue_common.model_update import to_update

_BOB = ParticipantKey("user:user-bob-4471")
_JOSH = ParticipantKey("user:user-josh-0001")
_CHAT = ChatId("agent-00000000000000000000000000000042")


def test_a_bubble_shows_the_first_part_of_a_message_and_the_first_paragraph_of_a_turn() -> None:
    assert bubble_text("  hi all  ") == "hi all"
    assert len(bubble_text("x" * 500)) == BUBBLE_TEXT_LIMIT
    assert first_paragraph("\n\nDone, see the file.\n\nIt holds the three cases.") == "Done, see the file."
    assert first_paragraph("one\nline\n\nmore") == "one\nline"
    assert first_paragraph("   \n\n  ") == ""


def test_every_chat_status_and_kind_is_spelled_the_same_on_the_wire() -> None:
    assert [activity_status_of(status).name for status in ChatStatus] == [status.name for status in ChatStatus]
    assert [activity_kind_of(kind).name for kind in ChatKind] == [kind.name for kind in ChatKind]


def test_the_chats_activity_report_lists_every_chat_with_its_status_and_kind() -> None:
    main = make_chat_snapshot("agent-1", name="Main-chat")
    lounge = make_chat_snapshot("agent-2", name="Lounge").model_copy_update(
        to_update(make_chat_snapshot("agent-2").field_ref().kind, ChatKind.ROOM),
        to_update(make_chat_snapshot("agent-2").field_ref().status, ChatStatus.STOPPED),
    )

    report = chats_activity_report([main, lounge])

    assert [(chat.chat_id, chat.title, chat.status, chat.kind) for chat in report.chats] == [
        ("agent-1", "Main chat", ChatActivityStatus.IDLE, ChatActivityKind.AGENT),
        ("agent-2", "Lounge", ChatActivityStatus.STOPPED, ChatActivityKind.ROOM),
    ]


def test_a_bubble_is_posted_to_the_shell_off_the_callers_thread() -> None:
    shell = FakeShell()
    feed = TrayFeed(shell=shell)

    feed.post_bubble(_BOB, _CHAT, "Lounge", "  @Josh look  ", "m-1", (_JOSH,)).result(timeout=10)

    assert shell.bubbles == [
        BubblePost(
            speaker=_BOB,
            chat_id=str(_CHAT),
            chat_title="Lounge",
            text="@Josh look",
            message_id="m-1",
            mentions=(_JOSH,),
        )
    ]
    feed.shutdown()


def test_a_post_after_shutdown_is_dropped_and_settles_without_raising() -> None:
    shell = FakeShell()
    feed = TrayFeed(shell=shell)
    feed.shutdown()

    feed.post_bubble(_BOB, _CHAT, "Lounge", "late", "m-9", ()).result(timeout=10)
    feed.report_chats([make_chat_snapshot("agent-1", name="Late")]).result(timeout=10)

    assert shell.bubbles == [] and shell.chats_activity == []
    feed.shutdown()


class _BrokenShell(FakeShell):
    """A shell whose bubble post raises something the shell client never answers itself."""

    def post_bubble(self, bubble: BubblePost) -> None:
        raise RuntimeError("the body could not be built")


def test_a_post_that_fails_for_a_reason_the_client_does_not_handle_is_logged(loguru_records: list[str]) -> None:
    feed = TrayFeed(shell=_BrokenShell())

    future = feed.post_bubble(_BOB, _CHAT, "Lounge", "hi", "m-1", ())
    assert isinstance(future.exception(timeout=10), RuntimeError)
    feed.shutdown()

    assert [record for record in loguru_records if record.startswith("WARNING")] == [
        "WARNING Posting a bubble to the shell's tray failed"
    ]


class _HeldShell(FakeShell):
    """A shell whose first chats-activity post waits, once begun, until the test lets it through."""

    entered: threading.Event = Field(default_factory=threading.Event, description="Set once the first post began")
    release: threading.Event = Field(default_factory=threading.Event, description="Set to let the first post through")

    def report_chats_activity(self, report: ChatsActivityReport) -> None:
        if not self.chats_activity:
            self.entered.set()
            self.release.wait(timeout=10)
        super().report_chats_activity(report)


def test_reports_queued_behind_one_not_yet_posted_collapse_into_the_last() -> None:
    shell = _HeldShell()
    feed = TrayFeed(shell=shell)
    first = feed.report_chats([make_chat_snapshot("agent-1", name="First")])
    assert shell.entered.wait(timeout=10)
    # While the first post is in flight: the second queues a post, and the third replaces what it will post.
    second = feed.report_chats([make_chat_snapshot("agent-2", name="Second")])
    third = feed.report_chats([make_chat_snapshot("agent-3", name="Third")])
    assert third.done() and not second.done()

    shell.release.set()
    first.result(timeout=10)
    second.result(timeout=10)
    feed.report_chats([make_chat_snapshot("agent-4", name="Fourth")]).result(timeout=10)

    assert [[chat.title for chat in report.chats] for report in shell.chats_activity] == [
        ["First"],
        ["Third"],
        ["Fourth"],
    ]
    feed.shutdown()


def test_the_last_report_is_posted_again_on_a_cadence_for_a_shell_restarted_since() -> None:
    shell = FakeShell()
    feed = TrayFeed(shell=shell, repost_interval_seconds=0.05)
    feed.report_chats([make_chat_snapshot("agent-1", name="First")]).result(timeout=10)
    assert len(shell.chats_activity) == 1

    feed.start()
    feed.start()
    wait_until_true(lambda: len(shell.chats_activity) >= 3, 10.0, "the report to be posted again")
    feed.shutdown()

    assert all(chat.title == "First" for report in shell.chats_activity for chat in report.chats)
