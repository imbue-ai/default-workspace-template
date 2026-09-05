"""The chat document over the shell's real Flask app: the dispatcher, the page, and the instances API."""

from pathlib import Path
from uuid import uuid4

import pytest
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import RecordingNudger
from app_instances.testing import free_port
from app_instances.testing import wait_until
from flask.testing import FlaskClient

from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.chat_document import _record_client_message_activity
from imbue.system_interface.chat_document import client_activity_report
from imbue.system_interface.chat_document import is_client_activity_reportable
from imbue.system_interface.documents import CHAT_AGENT_ID_META_NAME
from imbue.system_interface.documents import CHAT_SESSION_ID_META_NAME
from imbue.system_interface.documents import FRONTEND_BUILT_HEADER
from imbue.system_interface.models import SendMessageRequest
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import RecordingClientActivityShell
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import seed_agent_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster


def _agent_id() -> str:
    return f"agent-{uuid4().hex}"


def _state_with_chat(static_directory: Path, chat_id: str) -> tuple[SystemInterfaceState, AgentManager]:
    manager = AgentManager.build(WebSocketBroadcaster())
    seed_agent_state(manager, chat_id, name="Chat-1", labels={"display_name": "Chat 1"})
    manager.note_agent_list_known()
    state = build_test_state(agent_manager=manager)
    state.static_directory = static_directory
    return state, manager


def _write_bundle(static_directory: Path) -> None:
    static_directory.mkdir(parents=True, exist_ok=True)
    (static_directory / "index.html").write_text("<html><head></head><body>shell</body></html>")
    (static_directory / "chat.html").write_text("<html><head></head><body>chat</body></html>")
    (static_directory / "_static").mkdir(exist_ok=True)
    (static_directory / "_static" / "app_contract.js").write_text("export function connectToShell() {}\n")


def _client(tmp_path: Path, chat_id: str) -> tuple[FlaskClient, AgentManager]:
    _write_bundle(tmp_path)
    state, manager = _state_with_chat(tmp_path, chat_id)
    return create_application(state).test_client(), manager


def test_the_chat_page_carries_the_chats_identity(tmp_path: Path) -> None:
    chat_id = _agent_id()
    client, _ = _client(tmp_path, chat_id)

    response = client.get(f"/{chat_id}")

    assert response.status_code == 200
    assert response.headers[FRONTEND_BUILT_HEADER] == "true"
    assert response.headers["Cache-Control"] == "no-store"
    assert f'<meta name="{CHAT_AGENT_ID_META_NAME}" content="{chat_id}">' in response.text
    assert f'<meta name="{CHAT_SESSION_ID_META_NAME}" content="">' in response.text
    assert "chat</body>" in response.text


def test_a_subagent_page_names_its_session(tmp_path: Path) -> None:
    chat_id = _agent_id()
    client, _ = _client(tmp_path, chat_id)
    session_id = uuid4().hex

    response = client.get(f"/{chat_id}.{session_id}")

    assert response.status_code == 200
    assert f'<meta name="{CHAT_AGENT_ID_META_NAME}" content="{chat_id}">' in response.text
    assert f'<meta name="{CHAT_SESSION_ID_META_NAME}" content="{session_id}">' in response.text


def test_the_shell_still_serves_its_own_document(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _agent_id())
    response = client.get("/")
    assert response.status_code == 200
    assert "shell</body>" in response.text


def test_a_chat_page_without_a_bundle_is_the_not_built_placeholder(tmp_path: Path) -> None:
    chat_id = _agent_id()
    state, _ = _state_with_chat(tmp_path / "missing", chat_id)
    client = create_application(state).test_client()

    response = client.get(f"/{chat_id}")

    assert response.status_code == 200
    assert response.headers[FRONTEND_BUILT_HEADER] == "false"
    assert "not built" in response.text


def test_the_contract_module_is_served_for_any_origin(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _agent_id())
    response = client.get("/_static/app_contract.js")
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert response.mimetype == "text/javascript"
    assert "connectToShell" in response.text


def test_the_health_route_reports_the_bundle(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, _agent_id())
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": True}


def test_the_instances_api_lists_the_chat(tmp_path: Path) -> None:
    chat_id = _agent_id()
    client, _ = _client(tmp_path, chat_id)

    response = client.get("/_instances")

    assert response.status_code == 200
    (record,) = response.get_json()["instances"]
    assert record["key"] == chat_id
    assert record["url"] == f"/{chat_id}"
    assert record["title"] == "Chat 1"
    assert record["status"] == "idle"
    assert record["lifetime"] == "explicit"
    assert record["renameable"] is True


def test_the_instances_api_is_not_ready_before_the_first_discovery(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    state = build_test_state()
    state.static_directory = tmp_path
    client = create_application(state).test_client()

    response = client.get("/_instances")

    assert response.status_code == 503
    assert "detail" in response.get_json()


def test_a_subagent_create_answers_the_record_and_nudges(tmp_path: Path) -> None:
    chat_id = _agent_id()
    client, manager = _client(tmp_path, chat_id)
    nudger = RecordingNudger()
    manager.set_nudger(nudger)
    session_id = uuid4().hex

    response = client.post(
        "/_instances",
        json={"action": "subagent", "params": {"parent": chat_id, "session": session_id, "description": "Docs"}},
    )

    assert response.status_code == 201
    assert response.get_json()["instance"]["key"] == f"{chat_id}.{session_id}"
    assert response.get_json()["instance"]["title"] == "Subagent: Docs"
    assert nudger.nudge_count == 1
    listed = client.get("/_instances").get_json()["instances"]
    assert [record["key"] for record in listed] == [chat_id, f"{chat_id}.{session_id}"]


def test_a_location_report_is_refused_for_the_chat(tmp_path: Path) -> None:
    chat_id = _agent_id()
    client, _ = _client(tmp_path, chat_id)
    response = client.post(f"/_instances/{chat_id}/location", json={"path": "/elsewhere"})
    assert response.status_code == 400


def test_a_send_is_reported_to_the_shell_only_with_a_client_and_a_view(tmp_path: Path) -> None:
    agent_info = AgentInfo(
        id="agent-1", name="alice", state="RUNNING", agent_state_dir=tmp_path, claude_config_dir=tmp_path
    )
    framed = SendMessageRequest(message="hello", client_id="c1", active_layout="alpha", device_kind="desktop")
    assert is_client_activity_reportable(framed)
    assert not is_client_activity_reportable(SendMessageRequest(message="hello", client_id="c1"))
    assert not is_client_activity_reportable(SendMessageRequest(message="hello", active_layout="alpha"))
    assert client_activity_report(agent_info, framed) == {
        "client_id": "c1",
        "device_kind": "desktop",
        "view_id": "alpha",
        "kind": "message",
        "app": "chat",
        "key": "agent-1",
        "text": "hello",
    }


def test_a_framed_send_is_posted_to_the_shells_client_activity_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_info = AgentInfo(
        id="agent-1", name="alice", state="RUNNING", agent_state_dir=tmp_path, claude_config_dir=tmp_path
    )
    framed = SendMessageRequest(message="hello", client_id="c1", active_layout="alpha", device_kind="desktop")
    shell = RecordingClientActivityShell()
    port = free_port()
    with serve_in_background(LOOPBACK_HOST, port, shell.application):
        monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", f"http://{LOOPBACK_HOST}:{port}")
        _record_client_message_activity(agent_info, SendMessageRequest(message="unframed"))
        _record_client_message_activity(agent_info, framed)
        assert wait_until(lambda: shell.received == [client_activity_report(agent_info, framed)], timeout_seconds=5.0)
