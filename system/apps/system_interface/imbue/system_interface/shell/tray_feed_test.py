from datetime import datetime
from datetime import timezone

from workspace_layout.feed import ChatActivity
from workspace_layout.feed import ChatsActivityReport
from workspace_layout.primitives import ChatActivityKind
from workspace_layout.primitives import ChatActivityStatus
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import OWNER_PARTICIPANT_KEY
from workspace_layout.primitives import ParticipantKey
from workspace_layout.records import ClientRecord

from imbue.system_interface.shell.tray_feed import ChatsActivityStore
from imbue.system_interface.shell.tray_feed import speakers_client_ids

_NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
_BOB = ParticipantKey("user:user-bob-4471")
_JOSH = ParticipantKey("user:user-josh-0001")


def _client(client_id: str, participant: ParticipantKey | None) -> ClientRecord:
    return ClientRecord(id=ClientId(client_id), last_seen=_NOW, participant=participant)


_CLIENTS = (
    _client("bob-laptop", _BOB),
    _client("bob-phone", _BOB),
    _client("josh-laptop", _JOSH),
    _client("josh-before-sharing", OWNER_PARTICIPANT_KEY),
    _client("nobody", None),
)


def test_a_bubble_skips_every_client_recorded_as_the_speakers() -> None:
    assert speakers_client_ids(_BOB, False, _CLIENTS) == {"bob-laptop", "bob-phone"}


def test_the_owners_bubble_skips_the_clients_recorded_under_the_owner_key_too() -> None:
    assert speakers_client_ids(_JOSH, True, _CLIENTS) == {"josh-laptop", "josh-before-sharing"}
    # A visitor's bubble reaches the owner's clients, whichever key they carry.
    assert "josh-before-sharing" not in speakers_client_ids(_BOB, False, _CLIENTS)


def test_the_store_keeps_the_last_report_for_the_windows_that_connect_later() -> None:
    store = ChatsActivityStore()
    assert store.current() is None
    first = ChatsActivityReport(
        chats=(
            ChatActivity(
                chat_id="agent-1", title="Main chat", status=ChatActivityStatus.WORKING, kind=ChatActivityKind.AGENT
            ),
        )
    )
    second = ChatsActivityReport(chats=())

    store.replace(first)
    assert store.current() == first
    store.replace(second)
    assert store.current() == second
