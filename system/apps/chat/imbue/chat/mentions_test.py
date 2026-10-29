from collections.abc import Callable
from pathlib import Path

from pydantic import Field
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import ParticipantKind
from workspace_layout.roster import RosterDocument
from workspace_layout.testing import FakeShell
from workspace_layout.testing import roster_participant

from imbue.chat.agent_manager import AgentManager
from imbue.chat.authors import AuthorStore
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.mentions import MentionDeliverer
from imbue.chat.mentions import MentionHandler
from imbue.chat.mentions import MentionStore
from imbue.chat.mentions import PendingMention
from imbue.chat.mentions import mention_keys_of
from imbue.chat.mentions import mentions_filename
from imbue.chat.primitives import ChatId
from imbue.chat.roster_client import RosterClient
from imbue.chat.testing import RecordingMngrMessenger
from imbue.chat.testing import seed_agent_state
from imbue.chat.ws_broadcaster import WebSocketBroadcaster

_BOB = ParticipantKey("user:user-bob-4471")
_JOSH = ParticipantKey("user:user-josh-0001")
_LOUNGE = ChatId("room-0123456789abcdef")
_MAIN = ChatId("agent-00000000000000000000000000000001")


def test_mentions_are_appended_per_person_and_read_back_oldest_first(tmp_path: Path) -> None:
    store = MentionStore(root=tmp_path)

    store.record(_BOB, _LOUNGE, "m-1", _JOSH)
    store.record(_BOB, _MAIN, "m-2", _JOSH)
    store.record(_JOSH, _LOUNGE, "m-3", _BOB)

    assert [mention.model_dump() for mention in store.pending(_BOB)] == [
        {"chat_id": _LOUNGE, "message_id": "m-1", "sender": _JOSH},
        {"chat_id": _MAIN, "message_id": "m-2", "sender": _JOSH},
    ]
    assert store.pending(_JOSH) == (PendingMention(chat_id=_LOUNGE, message_id="m-3", sender=_BOB),)
    assert store.pending(ParticipantKey("owner")) == ()


def test_looking_at_a_chat_clears_only_that_chats_mentions(tmp_path: Path) -> None:
    store = MentionStore(root=tmp_path)
    store.record(_BOB, _LOUNGE, "m-1", _JOSH)
    store.record(_BOB, _MAIN, "m-2", _JOSH)

    assert store.clear(_BOB, _LOUNGE) is True
    assert [mention.chat_id for mention in store.pending(_BOB)] == [_MAIN]
    assert store.clear(_BOB, _LOUNGE) is False
    assert store.clear(_BOB, _MAIN) is True
    assert store.pending(_BOB) == () and not (tmp_path / mentions_filename(_BOB)).exists()


def test_the_file_is_named_after_the_key_itself() -> None:
    # A participant key is filename-safe, so the file needs no other name.
    assert mentions_filename(ParticipantKey("email:a.b@example.com")) == "email:a.b@example.com.jsonl"
    assert mentions_filename(_BOB) == "user:user-bob-4471.jsonl"


def test_a_requesters_mentions_are_read_and_cleared_under_every_key_of_theirs(tmp_path: Path) -> None:
    """The owner's mentions are recorded under the roster's key for them and asked for under ``owner`` by a page
    whose header carries no user id (the multiplayer spec, S12): both keys are read, and both are cleared."""
    store = MentionStore(root=tmp_path)
    store.record(_JOSH, _LOUNGE, "m-1", _BOB)
    store.record(ParticipantKey("owner"), _LOUNGE, "m-2", _BOB)
    store.record(ParticipantKey("owner"), _MAIN, "m-3", _BOB)

    both = (ParticipantKey("owner"), _JOSH)
    assert [mention.message_id for mention in store.pending_for(both)] == ["m-2", "m-3", "m-1"]
    assert store.clear_for(both, _LOUNGE) is True
    assert [mention.message_id for mention in store.pending_for(both)] == ["m-3"]
    assert store.clear_for(both, _LOUNGE) is False


def test_destroying_a_chat_drops_its_mentions_from_every_file(tmp_path: Path) -> None:
    store = MentionStore(root=tmp_path)
    store.record(_BOB, _LOUNGE, "m-1", _JOSH)
    store.record(_JOSH, _LOUNGE, "m-2", _BOB)
    store.record(_JOSH, _MAIN, "m-3", _BOB)

    assert store.clear_chat(_LOUNGE) is True
    assert store.pending(_BOB) == () and not (tmp_path / mentions_filename(_BOB)).exists()
    assert [mention.chat_id for mention in store.pending(_JOSH)] == [_MAIN]
    assert store.clear_chat(_LOUNGE) is False
    assert MentionStore(root=tmp_path / "never-written").clear_chat(_LOUNGE) is False


def test_the_owner_asks_under_both_of_their_keys_and_a_visitor_under_their_own() -> None:
    roster = RosterDocument(
        participants=(
            roster_participant(_JOSH, display_name="Josh", is_owner=True),
            roster_participant(_BOB, display_name="Bob"),
        )
    )
    anonymous_owner = RequestIdentity(owner=True)
    shared_owner = RequestIdentity(owner=True, user_id="user-josh-0001", email="josh@example.com")
    visitor = RequestIdentity(owner=False, user_id="user-bob-4471", email="bob@example.com")

    assert mention_keys_of(anonymous_owner, roster) == (ParticipantKey("owner"), _JOSH)
    assert mention_keys_of(shared_owner, roster) == (_JOSH, ParticipantKey("owner"))
    assert mention_keys_of(visitor, roster) == (_BOB,)
    assert mention_keys_of(anonymous_owner, RosterDocument(participants=())) == (ParticipantKey("owner"),)


class _RefusingSomeDeliverer(MentionDeliverer):
    """Delivers at once, the chats named refusing every send, as a chat whose account was signed out does."""

    refused: frozenset[ChatId] = Field(default=frozenset(), description="The chats that refuse every send")

    def deliver(self, chat_id: ChatId, delivered: str, message_id: str, on_refused: Callable[[], None]) -> None:
        if chat_id in self.refused:
            on_refused()


def test_a_room_waits_on_the_chats_its_mentions_reached_and_not_on_one_that_refused(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    reader = ChatId("agent-00000000000000000000000000000002")
    lounge = ChatId("agent-00000000000000000000000000000042")
    manager = AgentManager.build(broadcaster, messenger=RecordingMngrMessenger(), chat_files_root=tmp_path / "chats")
    manager.note_agent_list_known()
    seed_agent_state(manager, str(_MAIN), name="Main", labels={"display_name": "Main"})
    seed_agent_state(manager, str(reader), name="Newsreader", labels={"display_name": "Newsreader"})
    seed_agent_state(manager, str(lounge), name="Lounge", harness=HarnessType.ROOM, labels={"display_name": "Lounge"})
    assert manager.make_main_chat(_MAIN)
    roster = RosterDocument(
        participants=(
            roster_participant(_JOSH, display_name="Josh", is_owner=True),
            roster_participant(_BOB, display_name="Bob"),
            roster_participant(
                ParticipantKey("workspace-agent"),
                kind=ParticipantKind.WORKSPACE_AGENT,
                display_name="Atlas",
                handle="agent",
            ),
        )
    )
    handler = MentionHandler(
        agent_manager=manager,
        roster=RosterClient(shell=FakeShell(roster_document=roster)),
        mentions=MentionStore(root=tmp_path / "mentions"),
        authors=AuthorStore(root=tmp_path / "authors"),
        deliverer=_RefusingSomeDeliverer(refused=frozenset({reader})),
        on_pending_changed=lambda: None,
    )
    try:
        text = "@agent and @Newsreader, look"
        handler.act(handler.resolve(text), lounge, "Lounge", "m-1", text, _BOB, "Bob")

        snapshot = manager.get_chat_snapshot(str(lounge))
        assert snapshot is not None and snapshot.awaiting_chat_ids == (_MAIN,)
    finally:
        manager.stop()
