from pathlib import Path

from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import ParticipantKind
from workspace_layout.roster import RosterDocument
from workspace_layout.testing import roster_participant

from imbue.chat.agent_manager import AgentManager
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.lounge import LOUNGE_TITLE
from imbue.chat.lounge import LoungeKeeper
from imbue.chat.lounge import find_lounge
from imbue.chat.lounge import has_anyone_besides_the_owner
from imbue.chat.models import ProvisionalChatPhase
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.chat.testing import make_chat_snapshot
from imbue.chat.testing import seed_agent_state
from imbue.chat.testing import write_recording_mngr_binary
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.imbue_common.model_update import to_update

_LOUNGE_ID = "agent-00000000000000000000000000000042"


def _owner_only() -> RosterDocument:
    return RosterDocument(
        participants=(
            roster_participant(ParticipantKey("user:user-josh-0001"), display_name="Josh", is_owner=True),
            roster_participant(
                ParticipantKey("workspace-agent"), kind=ParticipantKind.WORKSPACE_AGENT, display_name="Atlas"
            ),
        )
    )


def _with(kind: ParticipantKind, key: str) -> RosterDocument:
    owner_only = _owner_only()
    return RosterDocument(
        participants=(*owner_only.participants, roster_participant(ParticipantKey(key), kind=kind, display_name="X"))
    )


def test_the_lounge_is_the_room_so_titled_and_not_an_agent_chat_of_that_name() -> None:
    agent_chat = make_chat_snapshot("agent-1", name="Lounge")
    room = make_chat_snapshot(_LOUNGE_ID, name="Lounge").model_copy_update(
        to_update(agent_chat.field_ref().kind, ChatKind.ROOM)
    )

    assert find_lounge([agent_chat]) is None
    assert find_lounge([agent_chat, room]) == ChatId(_LOUNGE_ID)


def test_anyone_invited_counts_but_the_owner_and_the_workspace_agent_do_not() -> None:
    assert not has_anyone_besides_the_owner(_owner_only())
    assert has_anyone_besides_the_owner(_with(ParticipantKind.PERSON, "user:user-bob-4471"))
    assert has_anyone_besides_the_owner(_with(ParticipantKind.INVITED, "email:carol@example.com"))
    assert has_anyone_besides_the_owner(_with(ParticipantKind.VISITING_AGENT, "agent:agent-7"))


def test_the_keeper_answers_the_room_there_is_and_makes_one_only_once(tmp_path: Path) -> None:
    mngr_binary, _log = write_recording_mngr_binary(tmp_path)
    manager = AgentManager.build(WebSocketBroadcaster(), mngr_binary=mngr_binary, chat_files_root=tmp_path / "chats")
    manager.note_agent_list_known()
    keeper = LoungeKeeper(agent_manager=manager)

    first = keeper.ensure()
    # Still being created, so not listed yet; the keeper remembers it rather than creating a second "Lounge".
    provisional = manager.get_provisional_chat(str(first))
    assert provisional is not None and provisional.name == LOUNGE_TITLE
    assert provisional.phase is ProvisionalChatPhase.CREATING
    assert keeper.ensure() == first

    seeded = AgentManager.build(WebSocketBroadcaster(), mngr_binary=mngr_binary, chat_files_root=tmp_path / "chats2")
    seeded.note_agent_list_known()
    seed_agent_state(seeded, _LOUNGE_ID, name="Lounge", harness=HarnessType.ROOM, labels={"display_name": "Lounge"})
    assert LoungeKeeper(agent_manager=seeded).ensure() == ChatId(_LOUNGE_ID)


def test_a_fresh_roster_makes_the_lounge_once_someone_besides_the_owner_is_in_it(tmp_path: Path) -> None:
    mngr_binary, _log = write_recording_mngr_binary(tmp_path)
    manager = AgentManager.build(WebSocketBroadcaster(), mngr_binary=mngr_binary, chat_files_root=tmp_path / "chats")
    keeper = LoungeKeeper(agent_manager=manager)

    # Nothing before the agent list is known (a Lounge may be listed in it), and nothing for the owner alone.
    keeper.consider_roster(_with(ParticipantKind.PERSON, "user:user-bob-4471"))
    manager.note_agent_list_known()
    keeper.consider_roster(_owner_only())
    assert manager.get_chat_snapshots() == [] and not manager.get_provisional_chats()

    keeper.consider_roster(_with(ParticipantKind.PERSON, "user:user-bob-4471"))

    [lounge] = manager.get_provisional_chats()
    assert lounge.name == LOUNGE_TITLE
