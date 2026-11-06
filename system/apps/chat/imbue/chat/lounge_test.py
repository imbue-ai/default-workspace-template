import threading
from pathlib import Path

import pytest
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import ParticipantKind
from workspace_layout.roster import RosterDocument
from workspace_layout.testing import FakeShell
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
from imbue.chat.roster_client import RosterClient
from imbue.chat.testing import make_chat_snapshot
from imbue.chat.testing import seed_agent_state
from imbue.chat.testing import wait_until_true
from imbue.chat.testing import write_recording_mngr_binary
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.imbue_common.model_update import to_update

_LOUNGE_ID = "agent-00000000000000000000000000000042"
_AGENT_LOUNGE_ID = "agent-00000000000000000000000000000043"


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


def test_the_lounge_is_the_chat_so_titled_of_either_kind() -> None:
    """The Lounge is found by title alone (the multiplayer spec, section 7.5): a Lounge converted into an agent chat,
    or a chat the user named so, is the Lounge, and a room under any other title is not."""
    agent_chat = make_chat_snapshot("agent-1", name="Lounge")
    room = make_chat_snapshot(_LOUNGE_ID, name="Lounge").model_copy_update(
        to_update(agent_chat.field_ref().kind, ChatKind.ROOM)
    )
    other_room = make_chat_snapshot("agent-3", name="Team").model_copy_update(
        to_update(agent_chat.field_ref().kind, ChatKind.ROOM)
    )

    assert find_lounge([other_room, agent_chat]) == ChatId("agent-1")
    assert find_lounge([other_room, room]) == ChatId(_LOUNGE_ID)
    assert find_lounge([other_room, make_chat_snapshot("agent-4", name="Lounge-2")]) is None


def test_the_lounge_is_found_by_the_rule_a_create_refuses_a_taken_name_by() -> None:
    """A chat called "lounge" or "Lounge " is the Lounge: the keeper's create of another would be refused as a
    taken name, so the title is matched the way the create matches it, not by the exact string."""
    lowercase = make_chat_snapshot("agent-1", name="lounge")
    padded = make_chat_snapshot("agent-2", name="Lounge ")
    plural = make_chat_snapshot("agent-3", name="Lounges")

    assert find_lounge((plural, lowercase)) == ChatId("agent-1")
    assert find_lounge((plural, padded)) == ChatId("agent-2")
    assert find_lounge((plural,)) is None


def test_anyone_invited_counts_but_the_owner_and_the_workspace_agent_do_not() -> None:
    assert not has_anyone_besides_the_owner(_owner_only())
    assert has_anyone_besides_the_owner(_with(ParticipantKind.PERSON, "user:user-bob-4471"))
    assert has_anyone_besides_the_owner(_with(ParticipantKind.INVITED, "email:carol@example.com"))
    assert has_anyone_besides_the_owner(_with(ParticipantKind.VISITING_AGENT, "agent:agent-7"))


def test_the_keeper_answers_the_chat_there_is_and_makes_a_room_only_once(tmp_path: Path) -> None:
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

    # An agent chat titled Lounge (a converted Lounge, or a chat so named) is the Lounge too: nothing is created.
    converted = AgentManager.build(
        WebSocketBroadcaster(), mngr_binary=mngr_binary, chat_files_root=tmp_path / "chats3"
    )
    converted.note_agent_list_known()
    seed_agent_state(converted, _AGENT_LOUNGE_ID, name="Lounge", labels={"display_name": "Lounge"})
    assert LoungeKeeper(agent_manager=converted).ensure() == ChatId(_AGENT_LOUNGE_ID)
    assert not converted.get_provisional_chats()
    manager.stop()
    seeded.stop()
    converted.stop()


def test_a_renamed_lounge_is_no_longer_the_lounge_and_the_next_need_makes_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Lounge is idempotent by title (the multiplayer spec, section 7.5): the room the keeper made is answered
    while its create is in flight, and once listed only for as long as it keeps the title."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    mngr_binary, _log = write_recording_mngr_binary(tmp_path)
    manager = AgentManager.build(WebSocketBroadcaster(), mngr_binary=mngr_binary, chat_files_root=tmp_path / "chats")
    manager.note_agent_list_known()
    keeper = LoungeKeeper(agent_manager=manager)

    first = keeper.ensure()
    wait_until_true(lambda: manager.get_provisional_chat(str(first)) is None, 10.0, "the Lounge's create to settle")
    assert keeper.ensure() == first
    manager.rename_chat(str(first), "Team")

    second = keeper.ensure()

    assert second != first
    provisional = manager.get_provisional_chat(str(second))
    assert provisional is not None and provisional.name == LOUNGE_TITLE
    manager.stop()


def test_a_roster_makes_the_lounge_once_someone_besides_the_owner_is_in_it(tmp_path: Path) -> None:
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
    manager.stop()


def test_the_keeper_polls_the_roster_and_makes_the_lounge_once_it_holds_an_invitation(tmp_path: Path) -> None:
    """The once-a-minute read (the multiplayer spec, section 7.5): the keeper's own poll reads the roster, finds
    someone besides the owner in it, and makes the Lounge."""
    mngr_binary, _log = write_recording_mngr_binary(tmp_path)
    manager = AgentManager.build(WebSocketBroadcaster(), mngr_binary=mngr_binary, chat_files_root=tmp_path / "chats")
    manager.note_agent_list_known()
    keeper = LoungeKeeper(agent_manager=manager, poll_interval_seconds=0.05)
    shell = FakeShell(roster_document=_with(ParticipantKind.PERSON, "user:user-bob-4471"))
    roster = RosterClient(shell=shell, cache_seconds=0.0)

    keeper.start(roster)
    keeper.start(roster)
    wait_until_true(lambda: bool(manager.get_provisional_chats()), 10.0, "the poll to make the Lounge")
    keeper.stop()

    [lounge] = manager.get_provisional_chats()
    assert lounge.name == LOUNGE_TITLE
    assert [thread.name for thread in threading.enumerate() if thread.name == "lounge-keeper"] == []
    manager.stop()
