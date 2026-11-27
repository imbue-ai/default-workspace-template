import threading
from pathlib import Path

from imbue.chat.agent_manager import AgentManager
from imbue.chat.chat_seed import is_first_run_seed
from imbue.chat.first_boot_welcome import OWES_WELCOME_CHAT_RELATIVE_PATH
from imbue.chat.first_boot_welcome import open_welcome_chat_owed_from_first_boot
from imbue.chat.main_chat import MAIN_CHAT_NAME
from imbue.chat.server import create_application
from imbue.chat.testing import build_test_state
from imbue.chat.ws_broadcaster import WebSocketBroadcaster


def _manager_that_knows_its_agents(chat_files_root: Path) -> AgentManager:
    manager = AgentManager.build(WebSocketBroadcaster(), chat_files_root=chat_files_root)
    manager.note_agent_list_known()
    return manager


def _owe_a_welcome_chat(workspace_root: Path) -> Path:
    owed = workspace_root / OWES_WELCOME_CHAT_RELATIVE_PATH
    owed.parent.mkdir(parents=True)
    owed.touch()
    return owed


def test_a_first_boot_opens_the_first_run_chat_once(tmp_path: Path) -> None:
    chats_root = tmp_path / "chats"
    manager = _manager_that_knows_its_agents(chats_root)
    owed = _owe_a_welcome_chat(tmp_path)

    open_welcome_chat_owed_from_first_boot(manager, owed, threading.Event())

    (welcome,) = manager.get_provisional_chats()
    assert welcome.name == MAIN_CHAT_NAME
    assert is_first_run_seed(chats_root / welcome.chat_id)
    assert manager.is_first_run_provider_step_pending() is True
    assert not owed.exists()

    # A later start of the app, with the debt settled, opens nothing more.
    open_welcome_chat_owed_from_first_boot(manager, owed, threading.Event())
    assert len(manager.get_provisional_chats()) == 1


def test_a_workspace_that_owes_no_welcome_chat_gets_none(tmp_path: Path) -> None:
    manager = _manager_that_knows_its_agents(tmp_path / "chats")

    open_welcome_chat_owed_from_first_boot(manager, tmp_path / OWES_WELCOME_CHAT_RELATIVE_PATH, threading.Event())

    assert manager.get_provisional_chats() == []


def test_a_client_that_seeded_first_leaves_the_one_chat_and_the_debt_settled(tmp_path: Path) -> None:
    manager = _manager_that_knows_its_agents(tmp_path / "chats")
    owed = _owe_a_welcome_chat(tmp_path)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    seeded = client.post("/api/chats/seed", json={"title": "Welcome", "turns": [], "is_first_run": True})
    assert seeded.status_code == 201

    open_welcome_chat_owed_from_first_boot(manager, owed, threading.Event())

    assert [chat.chat_id for chat in manager.get_provisional_chats()] == [seeded.get_json()["chat_id"]]
    assert not owed.exists()


def test_a_client_that_seeded_its_own_turns_first_keeps_its_chat_and_owes_no_provider_page(tmp_path: Path) -> None:
    chats_root = tmp_path / "chats"
    manager = _manager_that_knows_its_agents(chats_root)
    owed = _owe_a_welcome_chat(tmp_path)
    seeded = manager.seed_chat(MAIN_CHAT_NAME, ())

    open_welcome_chat_owed_from_first_boot(manager, owed, threading.Event())

    assert [chat.chat_id for chat in manager.get_provisional_chats()] == [seeded.chat_id]
    assert not is_first_run_seed(chats_root / seeded.chat_id)
    assert manager.is_first_run_provider_step_pending() is False
    assert not owed.exists()


def test_a_client_that_seeds_after_the_workspace_did_is_answered_the_same_chat(tmp_path: Path) -> None:
    manager = _manager_that_knows_its_agents(tmp_path / "chats")
    owed = _owe_a_welcome_chat(tmp_path)
    open_welcome_chat_owed_from_first_boot(manager, owed, threading.Event())
    (welcome,) = manager.get_provisional_chats()
    client = create_application(build_test_state(agent_manager=manager)).test_client()

    seeded = client.post("/api/chats/seed", json={"title": "Welcome", "turns": [], "is_first_run": True})

    assert seeded.status_code == 201
    assert seeded.get_json()["chat_id"] == welcome.chat_id
    assert len(manager.get_provisional_chats()) == 1


def test_the_debt_stays_while_the_app_stops_before_it_knows_its_agents(tmp_path: Path) -> None:
    manager = AgentManager.build(WebSocketBroadcaster(), chat_files_root=tmp_path / "chats")
    owed = _owe_a_welcome_chat(tmp_path)
    is_stopping = threading.Event()
    is_stopping.set()

    open_welcome_chat_owed_from_first_boot(manager, owed, is_stopping)

    assert owed.exists()
    assert manager.get_provisional_chats() == []


def test_the_debt_stays_when_the_chat_cannot_be_written(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "taken"
    not_a_directory.write_text("")
    manager = _manager_that_knows_its_agents(not_a_directory / "chats")
    owed = _owe_a_welcome_chat(tmp_path)

    open_welcome_chat_owed_from_first_boot(manager, owed, threading.Event())

    assert owed.exists()
    assert manager.get_provisional_chats() == []
