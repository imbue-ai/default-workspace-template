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
    assert speakers_client_ids(_BOB, _JOSH, _CLIENTS) == {"bob-laptop", "bob-phone"}
    # A visitor's bubble reaches the owner's clients, whichever key they carry.
    assert speakers_client_ids(_BOB, _JOSH, _CLIENTS).isdisjoint({"josh-laptop", "josh-before-sharing"})


def test_the_owners_bubble_skips_the_clients_recorded_under_either_of_their_keys() -> None:
    """Once the owner's heartbeats name an account, a bubble of theirs under that key or under the synthetic one (a
    script's send) skips the clients recorded under both; on an unshared workspace only the synthetic key's exist."""
    assert speakers_client_ids(_JOSH, _JOSH, _CLIENTS) == {"josh-laptop", "josh-before-sharing"}
    assert speakers_client_ids(OWNER_PARTICIPANT_KEY, _JOSH, _CLIENTS) == {"josh-laptop", "josh-before-sharing"}
    assert speakers_client_ids(OWNER_PARTICIPANT_KEY, OWNER_PARTICIPANT_KEY, _CLIENTS) == {"josh-before-sharing"}


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
