"""Tests for the Flask server."""

import io
import json
import os
import queue
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import quote

import pytest
from flask import Flask
from flask import Response
from flask import request as flask_request
from flask.testing import FlaskClient
from mngr_cli_contract.contract import assert_mngr_argv_valid
from oom_priority import bands

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.errors import AgentStartError
from imbue.mngr_claude.claude_config import get_managed_settings_path
from imbue.system_interface import client_activity
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import state_of
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.member_titles import MAX_MEMBER_TITLE_LENGTH
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import AppEntry
from imbue.system_interface.oom_prioritizer import ChatOomPrioritizer
from imbue.system_interface.projects import EVERYTHING_VIEW_ID
from imbue.system_interface.projects import EVERYTHING_VIEW_NAME
from imbue.system_interface.server import _DEFAULT_TAIL_COUNT
from imbue.system_interface.server import _FORWARD_PORT_SCRIPT
from imbue.system_interface.server import _build_destroy_command
from imbue.system_interface.server import _handle_client_state_message
from imbue.system_interface.server import _stream_filtered_events
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import RecordingMngrMessenger
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import close_ws
from imbue.system_interface.testing import open_ws
from imbue.system_interface.testing import serve_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# Generous: the first receive occasionally exceeded the previous 5.0s cap on a
# loaded machine (~1-in-8 locally, failing as ``json.loads(None)``) even though
# passing runs complete in well under a second -- the wait is pure scheduling
# delay, so a bigger cap costs nothing when healthy.
_WS_RECEIVE_TIMEOUT = 15.0


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def app(config: Config) -> Flask:
    return create_application(build_test_state(config=config))


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_index_returns_html_when_static_exists(client: FlaskClient, tmp_path: Path) -> None:
    """When the static dir has index.html, the server serves it."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>test</body></html>")

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", static_dir):
        test_client = create_application(build_test_state()).test_client()
        response = test_client.get("/")
        assert response.status_code == 200
        assert "test" in response.text


def test_index_returns_not_built_when_no_static(client: FlaskClient, tmp_path: Path) -> None:
    """When static dir has no index.html, show a helpful message."""
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", empty_dir):
        test_client = create_application(build_test_state()).test_client()
        response = test_client.get("/")
        assert response.status_code == 200
        assert "npm run build" in response.text


def test_list_agents_endpoint(client: FlaskClient) -> None:
    """The agents endpoint returns agent data."""
    with patch("imbue.system_interface.server.discover_agents") as mock_discover:
        mock_discover.return_value = [
            AgentInfo(
                id="agent-123",
                name="test-agent",
                state="RUNNING",
                agent_state_dir=Path("/tmp/test"),
                claude_config_dir=Path("/tmp/.claude"),
            )
        ]
        response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["agents"]) == 1
    assert data["agents"][0]["name"] == "test-agent"
    assert data["agents"][0]["state"] == "RUNNING"


def test_get_events_for_unknown_agent(client: FlaskClient) -> None:
    """Getting events for a nonexistent agent returns 404."""
    with patch("imbue.system_interface.server.discover_agents", return_value=[]):
        response = client.get("/api/agents/nonexistent/events")
    assert response.status_code == 404


def test_send_message_for_unknown_agent(client: FlaskClient) -> None:
    """Sending a message to a nonexistent agent returns 404."""
    with patch("imbue.system_interface.server.discover_agents", return_value=[]):
        response = client.post("/api/agents/nonexistent/message", json={"message": "hello"})
    assert response.status_code == 404


def _upload_relative_path(stored_path: str) -> str:
    """Extract the ``<subdir>/<name>`` part of an absolute upload path."""
    return stored_path.split("/uploads/", 1)[1]


def test_upload_attachment_stores_file_and_returns_path(client: FlaskClient) -> None:
    """Uploading a file stores it under data/uploads/ and returns its path and size."""
    response = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(b"image-bytes"), "diagram.png")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    data = response.get_json()
    assert "/uploads/" in data["path"]
    assert data["path"].endswith("/diagram.png")
    assert data["size"] == len(b"image-bytes")
    assert Path(data["path"]).read_bytes() == b"image-bytes"


def test_upload_attachment_without_file_returns_400(client: FlaskClient) -> None:
    """Posting with no file part is a 400."""
    response = client.post("/api/uploads", data={}, content_type="multipart/form-data")

    assert response.status_code == 400


def test_serve_attachment_returns_stored_bytes(client: FlaskClient) -> None:
    """A stored attachment can be fetched back for preview."""
    upload = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(b"hello-bytes"), "note.txt")},
        content_type="multipart/form-data",
    )
    relative_path = _upload_relative_path(upload.get_json()["path"])

    response = client.get(f"/api/uploads/{relative_path}")

    assert response.status_code == 200
    assert response.data == b"hello-bytes"


def test_serve_attachment_missing_returns_404(client: FlaskClient) -> None:
    """Fetching an unknown attachment is a 404."""
    response = client.get("/api/uploads/deadbeef/missing.png")

    assert response.status_code == 404


def test_delete_attachment_removes_stored_file(client: FlaskClient) -> None:
    """Deleting an attachment removes it from disk and from later fetches."""
    upload = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(b"bye-bytes"), "remove-me.txt")},
        content_type="multipart/form-data",
    )
    stored_path = upload.get_json()["path"]
    relative_path = _upload_relative_path(stored_path)

    delete_response = client.delete(f"/api/uploads/{relative_path}")

    assert delete_response.status_code == 200
    assert not Path(stored_path).exists()
    assert client.get(f"/api/uploads/{relative_path}").status_code == 404


def test_delete_attachment_missing_is_ok(client: FlaskClient) -> None:
    """Deleting an unknown attachment still reports success (idempotent)."""
    response = client.delete("/api/uploads/deadbeef/missing.png")

    assert response.status_code == 200


def test_get_events_with_session_files(client: FlaskClient, tmp_path: Path) -> None:
    """Getting events for an agent with session files returns parsed events."""
    # Set up agent state dir with session history
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)

    # Create a session file
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)

    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Hello"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "uuid-2",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": "Hi!"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        + "\n"
    )

    # Write session history
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/events")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["events"]) == 2
    assert data["events"][0]["type"] == "user_message"
    assert data["events"][0]["content"] == "Hello"
    assert data["events"][1]["type"] == "assistant_message"
    assert data["events"][1]["text"] == "Hi!"


def test_get_events_caps_initial_load_to_tail(client: FlaskClient, tmp_path: Path) -> None:
    """The no-`before` events response is capped to the most recent N events,
    and older events remain reachable via the `before` backfill branch (issue I)."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)

    total_events = _DEFAULT_TAIL_COUNT + 10
    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        "".join(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"uuid-{i:03d}",
                    "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                    "message": {"role": "user", "content": f"Message {i}"},
                }
            )
            + "\n"
            for i in range(total_events)
        )
    )
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )

    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/events")
        assert response.status_code == 200
        body = response.get_json()
        events = body["events"]
        # Only the most recent _DEFAULT_TAIL_COUNT events are returned.
        assert len(events) == _DEFAULT_TAIL_COUNT
        assert events[0]["content"] == f"Message {total_events - _DEFAULT_TAIL_COUNT}"
        assert events[-1]["content"] == f"Message {total_events - 1}"
        # offset + total place the tail window in the full conversation: the first
        # tail event sits at index (total - tail), so offset > 0 tells the client
        # there is older history above to page in.
        assert body["total"] == total_events
        assert body["offset"] == total_events - _DEFAULT_TAIL_COUNT

        # Older events are still reachable by paging backwards from the oldest
        # event in the initial tail.
        oldest_in_tail = events[0]["event_id"]
        backfill = client.get(f"/api/agents/agent-123/events?before={oldest_in_tail}")
        assert backfill.status_code == 200
        backfill_body = backfill.get_json()
        backfill_events = backfill_body["events"]
        assert len(backfill_events) == total_events - _DEFAULT_TAIL_COUNT
        assert backfill_events[0]["content"] == "Message 0"
        assert backfill_events[-1]["content"] == f"Message {total_events - _DEFAULT_TAIL_COUNT - 1}"
        # The page reached the very first event (offset 0 => no more history above).
        assert backfill_body["offset"] == 0
        assert backfill_body["total"] == total_events

        # A jump lands a window at an arbitrary global offset in one request,
        # rather than paging through everything before it.
        jump = client.get("/api/agents/agent-123/events?offset=5&limit=4")
        assert jump.status_code == 200
        jump_body = jump.get_json()
        assert [e["content"] for e in jump_body["events"]] == [f"Message {i}" for i in range(5, 9)]
        assert jump_body["offset"] == 5

        # From that jumped window the client can page *newer* (toward the tail).
        after_id = jump_body["events"][-1]["event_id"]
        forward = client.get(f"/api/agents/agent-123/events?after={after_id}&limit=3")
        assert forward.status_code == 200
        forward_body = forward.get_json()
        assert [e["content"] for e in forward_body["events"]] == [f"Message {i}" for i in range(9, 12)]
        assert forward_body["offset"] == 9

        # A non-positive limit must not defeat the cap (``[-0:]`` would return
        # the whole list); it falls back to the default tail count.
        zero_limit = client.get("/api/agents/agent-123/events?limit=0")
        assert zero_limit.status_code == 200
        assert len(zero_limit.get_json()["events"]) == _DEFAULT_TAIL_COUNT


def test_send_message_success() -> None:
    """Sending a message to a known agent addresses it by id and succeeds."""
    agent_id = "agent-00000000000000000000000000000001"
    agent_info = AgentInfo(
        id=agent_id,
        name="test-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.post(f"/api/agents/{agent_id}/message", json={"message": "hello"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    # The endpoint routes through AgentManager.send_message_to_agent, which addresses
    # the agent by id (the live cache supplies the known location as the 3rd arg).
    assert messenger.sent == [(agent_id, "hello")]


def _model_settings_agent_info(agent_id: str, tmp_path: Path, settings: dict[str, Any] | None) -> AgentInfo:
    """An AgentInfo whose claude_config_dir holds a settings.json (or none, when settings is None)."""
    config_dir = tmp_path / "claude_config"
    config_dir.mkdir(exist_ok=True)
    if settings is not None:
        (config_dir / "settings.json").write_text(json.dumps(settings))
    return AgentInfo(
        id=agent_id,
        name="test-agent",
        state="RUNNING",
        agent_state_dir=tmp_path / "state",
        claude_config_dir=config_dir,
    )


def test_get_model_settings_reflects_settings_json(client: FlaskClient, tmp_path: Path) -> None:
    """The endpoint returns the agent's stored model + fast mode and the catalog."""
    agent_id = "agent-00000000000000000000000000000002"
    agent_info = _model_settings_agent_info(agent_id, tmp_path, {"model": "opus[1m]", "fastMode": True})
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.get(f"/api/agents/{agent_id}/model-settings")

    assert response.status_code == 200
    data = response.get_json()
    assert data["model"] == "opus[1m]"
    assert data["fast_mode"] is True
    # Opus supports fast mode, so the toggle is offered.
    assert data["fast_mode_supported"] is True
    option_ids = [option["id"] for option in data["options"]]
    assert option_ids == ["fable", "opus[1m]", "sonnet", "haiku"]


def test_get_model_settings_non_opus_hides_fast_toggle(client: FlaskClient, tmp_path: Path) -> None:
    """A non-Opus model reports fast mode unsupported (frontend hides the toggle)."""
    agent_id = "agent-00000000000000000000000000000003"
    agent_info = _model_settings_agent_info(agent_id, tmp_path, {"model": "sonnet"})
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.get(f"/api/agents/{agent_id}/model-settings")

    data = response.get_json()
    assert data["model"] == "sonnet"
    assert data["fast_mode"] is False
    assert data["fast_mode_supported"] is False


def test_get_model_settings_unknown_agent_returns_404(client: FlaskClient) -> None:
    with patch("imbue.system_interface.server._find_agent", return_value=None):
        response = client.get("/api/agents/nonexistent/model-settings")
    assert response.status_code == 404


def test_set_model_sends_slash_command() -> None:
    """POSTing a model switch sends the running agent a `/model <id>` command."""
    agent_id = "agent-00000000000000000000000000000004"
    agent_info = AgentInfo(
        id=agent_id,
        name="test-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.post(f"/api/agents/{agent_id}/model", json={"model": "sonnet"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert messenger.sent == [(agent_id, "/model sonnet")]


def test_set_model_rejects_unknown_model() -> None:
    """An id outside the catalog is a 400 and no command is sent."""
    agent_id = "agent-00000000000000000000000000000005"
    agent_info = AgentInfo(
        id=agent_id,
        name="test-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.post(f"/api/agents/{agent_id}/model", json={"model": "gpt-4"})

    assert response.status_code == 400
    assert messenger.sent == []


def test_set_model_unknown_agent_returns_404(client: FlaskClient) -> None:
    with patch("imbue.system_interface.server._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/model", json={"model": "sonnet"})
    assert response.status_code == 404


def test_set_fast_mode_sends_on_and_off(tmp_path: Path) -> None:
    """POSTing fast mode sends the running agent a `/fast on` or `/fast off` command."""
    agent_id = "agent-00000000000000000000000000000006"
    agent_info = _model_settings_agent_info(agent_id, tmp_path, {"model": "opus[1m]"})
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        on = client.post(f"/api/agents/{agent_id}/fast", json={"enabled": True})
        off = client.post(f"/api/agents/{agent_id}/fast", json={"enabled": False})

    assert on.status_code == 200
    assert off.status_code == 200
    assert messenger.sent == [(agent_id, "/fast on"), (agent_id, "/fast off")]


def test_set_fast_mode_unknown_agent_returns_404(client: FlaskClient) -> None:
    with patch("imbue.system_interface.server._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/fast", json={"enabled": True})
    assert response.status_code == 404


def test_model_settings_prefers_managed_settings_over_user_settings(client: FlaskClient, tmp_path: Path) -> None:
    """mngr passes the managed file via --settings, which Claude layers above the
    shared user settings -- so a freshly launched agent reports the provisioned
    value, not the stale one the shared config happens to carry."""
    agent_id = "agent-00000000000000000000000000000020"
    agent_info = _model_settings_agent_info(agent_id, tmp_path, {"model": "opus[1m]"})
    managed_path = get_managed_settings_path(agent_info.agent_state_dir)
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    managed_path.write_text(json.dumps({"fastMode": True}))

    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.get(f"/api/agents/{agent_id}/model-settings")

    # The user settings file has no fastMode key at all, which on its own reads as
    # off; the managed overlay is what makes this agent fast.
    assert response.get_json()["fast_mode"] is True


def test_setting_fast_mode_records_it_where_the_next_launch_reads_it(tmp_path: Path) -> None:
    """`/fast off` deletes the key instead of writing false, so the toggle is written
    into the agent's own launch settings -- which is both what the picker reads back
    and what the agent comes back with if it restarts."""
    agent_id = "agent-00000000000000000000000000000021"
    agent_info = _model_settings_agent_info(agent_id, tmp_path, {"model": "opus[1m]"})
    managed_path = get_managed_settings_path(agent_info.agent_state_dir)
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    # The agent was provisioned fast, and mngr's hooks share the file.
    managed_path.write_text(json.dumps({"hooks": {"SessionStart": ["mark-active"]}, "fastMode": True}))

    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        assert client.get(f"/api/agents/{agent_id}/model-settings").get_json()["fast_mode"] is True
        assert client.post(f"/api/agents/{agent_id}/fast", json={"enabled": False}).status_code == 200
        assert client.get(f"/api/agents/{agent_id}/model-settings").get_json()["fast_mode"] is False

    # On disk, so a restart of this service or of the agent reports the same thing --
    # and mngr's hooks are still there.
    assert json.loads(managed_path.read_text()) == {
        "hooks": {"SessionStart": ["mark-active"]},
        "fastMode": False,
    }


def test_setting_fast_mode_reports_settings_it_cannot_record(tmp_path: Path) -> None:
    """The running session took the command but the change will not outlive it, so
    the caller is told rather than shown a success it cannot rely on."""
    agent_id = "agent-00000000000000000000000000000022"
    agent_info = _model_settings_agent_info(agent_id, tmp_path, {"model": "opus[1m]"})
    managed_path = get_managed_settings_path(agent_info.agent_state_dir)
    managed_path.parent.mkdir(parents=True, exist_ok=True)
    managed_path.write_text("{not valid json")

    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
        response = client.post(f"/api/agents/{agent_id}/fast", json={"enabled": False})

    assert response.status_code == 500
    # Whatever mngr had in there is untouched rather than replaced.
    assert managed_path.read_text() == "{not valid json"


def test_workspace_fast_mode_starts_undecided_and_records_an_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The prompt is owed until answered, and the answer survives for later chats."""
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    client = create_application(build_test_state()).test_client()

    assert client.get("/api/workspace/fast-mode").get_json()["fast_mode"] is None

    recorded = client.post("/api/workspace/fast-mode", json={"enabled": False}).get_json()
    assert recorded["fast_mode"] is False

    # A later reader (a new chat create, another browser) sees the same answer.
    assert client.get("/api/workspace/fast-mode").get_json()["fast_mode"] is False


def test_workspace_fast_mode_can_keep_fast_mode_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Answering "keep it" must also stick, or the prompt would reappear forever."""
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))
    client = create_application(build_test_state()).test_client()

    client.post("/api/workspace/fast-mode", json={"enabled": True})
    assert client.get("/api/workspace/fast-mode").get_json()["fast_mode"] is True


def _manager_with_capturing_prioritizer(writes: list[tuple[int, int]], pids: dict[str, int]) -> AgentManager:
    """An AgentManager whose OOM prioritizer captures its band writes.

    The prioritizer collaborator is swapped for one wired to a fake pid resolver
    and a capturing ``set_adj`` (mirrors how other tests seed ``_agents``), so a
    POST to ``/api/activity`` drives the real endpoint -> ``record_activity`` ->
    prioritizer -> ``get_chat_agent_ids`` -> ``set_adj`` path without touching
    ``/proc``.
    """
    manager = AgentManager.build(WebSocketBroadcaster())
    manager._oom_prioritizer = ChatOomPrioritizer(
        list_chat_agent_ids=manager.get_chat_agent_ids,
        resolve_pid=lambda cid: pids.get(cid),
        set_adj=lambda pid, adj: (writes.append((pid, adj)), True)[1],
    )
    return manager


def test_activity_endpoint_retags_a_chat_from_the_report() -> None:
    """A well-formed report flows through to re-tag the reported chat's band."""
    writes: list[tuple[int, int]] = []
    manager = _manager_with_capturing_prioritizer(writes, pids={"chat": 4242})
    with manager._lock:
        manager._agents["chat"] = AgentStateItem(
            id="chat", name="chat", state="RUNNING", labels={"user_created": "true"}, work_dir=None
        )
    client = create_application(build_test_state(agent_manager=manager)).test_client()

    response = client.post("/api/activity", json={"open": ["chat"], "visible": ["chat"], "messaged": "chat"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    # Open + visible + most-recently messaged -> the most-protected chat band.
    assert writes == [(4242, bands.chat_agent_oom_score_adj(is_open=True, is_visible=True, recency_rank=0))]


def test_activity_endpoint_defaults_missing_fields() -> None:
    """Omitted fields default to empty sets / no message, so a bare ping is valid
    and re-tags a known chat as the most-expendable (closed, unmessaged) band."""
    writes: list[tuple[int, int]] = []
    manager = _manager_with_capturing_prioritizer(writes, pids={"chat": 4242})
    with manager._lock:
        manager._agents["chat"] = AgentStateItem(
            id="chat", name="chat", state="RUNNING", labels={"user_created": "true"}, work_dir=None
        )
    client = create_application(build_test_state(agent_manager=manager)).test_client()

    response = client.post("/api/activity", json={})

    assert response.status_code == 200
    assert writes == [(4242, bands.chat_agent_oom_score_adj(is_open=False, is_visible=False, recency_rank=None))]


def test_interrupt_agent_returns_404_for_unknown_agent(client: FlaskClient) -> None:
    """Interrupting a nonexistent agent returns 404."""
    with patch("imbue.system_interface.server._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/interrupt")
    assert response.status_code == 404


def test_interrupt_agent_success(client: FlaskClient) -> None:
    """Interrupting an agent restarts it via mngr and returns 200."""
    agent_info = AgentInfo(
        id="agent-123",
        name="claude-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    fake_result = FinishedProcess(
        returncode=0,
        stdout="Restarted agent: claude-agent",
        stderr="",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )
    with (
        patch("imbue.system_interface.server._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.server.run_local_command_modern_version",
            return_value=fake_result,
        ) as mock_run,
        patch.object(AgentManager, "reset_activity_state") as mock_reset,
    ):
        response = client.post("/api/agents/agent-123/interrupt")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert mock_run.call_args.kwargs["command"] == [
        "mngr",
        "start",
        "claude-agent",
        "--restart",
        "--no-resume",
    ]
    # After a successful restart the endpoint resets the agent's activity
    # state so the indicator clears instead of staying pinned at THINKING.
    mock_reset.assert_called_once_with("agent-123")


def test_interrupt_agent_rejects_is_primary_agent(client: FlaskClient) -> None:
    """POST /api/agents/<id>/interrupt returns 400 for the services agent.

    Restarting the is_primary agent would stop the workspace services. The
    frontend hides such agents; this server-side guard protects direct callers.
    """
    services_agent = AgentInfo(
        id="services-1",
        name="system-services",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
        labels={"is_primary": "true", "workspace": "my-ws"},
    )
    with (
        patch("imbue.system_interface.server._find_agent", return_value=services_agent),
        patch("imbue.system_interface.server.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/services-1/interrupt")

    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    # The guard runs before the restart subprocess, so mngr is never invoked.
    mock_run.assert_not_called()


def test_interrupt_agent_returns_500_on_failure(client: FlaskClient) -> None:
    """If the mngr restart command exits non-zero, return 500 with its stderr."""
    agent_info = AgentInfo(
        id="agent-123",
        name="claude-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    fake_result = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="mngr start failed",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )
    with (
        patch("imbue.system_interface.server._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.server.run_local_command_modern_version",
            return_value=fake_result,
        ),
    ):
        response = client.post("/api/agents/agent-123/interrupt")

    assert response.status_code == 500
    assert response.get_json()["detail"] == "Failed to interrupt agent 'claude-agent': mngr start failed"


def test_list_layouts_exposes_defaults(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh workspace lists the two default layout names, both empty."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/layouts")

    assert response.status_code == 200
    body = response.get_json()
    assert [layout["slug"] for layout in body["layouts"]] == ["desktop", "mobile"]
    assert all(layout["has_content"] is False for layout in body["layouts"])
    assert body["last_active_slug"] == "desktop"


def test_get_empty_layout_returns_null_content(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered-but-never-saved layout reports null content (fresh state)."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/layouts/mobile")

    assert response.status_code == 200
    assert response.get_json() == {"slug": "mobile", "display_name": "mobile", "layout": None}


def test_get_unknown_layout_returns_404(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/layouts/nonexistent")

    assert response.status_code == 404


def test_autosave_and_get_layout_round_trips(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    layout_data = {"dockview": {"panels": {}}, "panelParams": {"chat-1": {"panelType": "chat"}}}
    save_response = client.post("/api/layouts/desktop", json={"layout": layout_data, "client_id": "client-1"})
    assert save_response.status_code == 200
    assert save_response.get_json()["status"] == "ok"

    get_response = client.get("/api/layouts/desktop")
    assert get_response.status_code == 200
    assert get_response.get_json()["layout"] == layout_data
    assert (tmp_path / "agents" / "agent-123" / "workspace_layout" / "layouts" / "desktop.json").exists()


def test_autosave_unknown_layout_returns_404(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An autosave against a just-deleted layout must not resurrect it."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.post("/api/layouts/gone", json={"layout": {}, "client_id": "client-1"})

    assert response.status_code == 404


def test_save_layout_as_creates_and_reports_slug(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save-as slugifies the display name server-side and registers the layout."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    response = client.post(
        "/api/layouts",
        json={"display_name": "My Fancy Setup!", "layout": {"dockview": {}}, "client_id": "client-1"},
    )
    assert response.status_code == 200
    assert response.get_json() == {"slug": "my-fancy-setup", "display_name": "My Fancy Setup!"}

    list_response = client.get("/api/layouts")
    slugs = [layout["slug"] for layout in list_response.get_json()["layouts"]]
    assert "my-fancy-setup" in slugs


def test_save_layout_as_rejects_slug_conflict(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different display names that shorten to the same slug conflict."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    first = client.post("/api/layouts", json={"display_name": "My Setup", "layout": {}, "client_id": "c1"})
    assert first.status_code == 200
    second = client.post("/api/layouts", json={"display_name": "my setup", "layout": {}, "client_id": "c1"})

    assert second.status_code == 409
    assert "conflicts" in second.get_json()["detail"]


def test_save_layout_as_rejects_unusable_name(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.post("/api/layouts", json={"display_name": "!!!", "layout": {}, "client_id": "c1"})

    assert response.status_code == 400


def test_delete_layout_and_last_layout_guard(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting works down to the last layout, which is protected."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    delete_mobile = client.post("/api/layouts/mobile/delete")
    assert delete_mobile.status_code == 200
    assert delete_mobile.get_json()["fallback_layout_slug"] == "desktop"

    delete_last = client.post("/api/layouts/desktop/delete")
    assert delete_last.status_code == 409

    delete_unknown = client.post("/api/layouts/mobile/delete")
    assert delete_unknown.status_code == 404


def test_legacy_layout_json_migrates_to_desktop(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-named-layouts layout.json becomes the desktop layout's content."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    layout_dir = tmp_path / "agents" / "agent-123" / "workspace_layout"
    layout_dir.mkdir(parents=True)
    legacy_content = {"dockview": {"panels": {}}, "panelParams": {"chat-old": {"panelType": "chat"}}}
    (layout_dir / "layout.json").write_text(json.dumps(legacy_content))

    response = client.get("/api/layouts/desktop")

    assert response.status_code == 200
    assert response.get_json()["layout"] == legacy_content
    assert not (layout_dir / "layout.json").exists()
    assert (layout_dir / "layout.json.migrated").exists()


def test_send_message_records_client_activity_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A message POST carrying client metadata appends a message event."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    messenger = RecordingMngrMessenger(sent=[], succeeds=True)
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    app = create_application(build_test_state(agent_manager=manager))
    chat_agent_id = "agent-00000000000000000000000000000002"
    agent_info = AgentInfo(
        id=chat_agent_id,
        name="chat-agent",
        state="RUNNING",
        agent_state_dir=tmp_path / "agents" / chat_agent_id,
        claude_config_dir=tmp_path / ".claude",
    )
    try:
        test_client = app.test_client()
        with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
            response = test_client.post(
                f"/api/agents/{chat_agent_id}/message",
                json={
                    "message": "hello " + "x" * 600,
                    "client_id": "client-7",
                    "active_layout": "mobile",
                    "device_kind": "mobile",
                },
            )
        assert response.status_code == 200

        events_path = (
            tmp_path / "agents" / "agent-123" / "workspace_layout" / "events" / "client_activity" / "events.jsonl"
        )
        assert events_path.exists()
        event = json.loads(events_path.read_text().splitlines()[0])
        assert event["type"] == "message"
        assert event["client_id"] == "client-7"
        assert event["layout_slug"] == "mobile"
        assert event["device_kind"] == "mobile"
        assert event["agent_name"] == "chat-agent"
        assert event["is_message_truncated"] is True
        assert len(event["message_text"]) == 500
    finally:
        manager.stop()


def test_terminal_banner_defaults_to_not_dismissed(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no saved preference, the terminal banner reports not-dismissed."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/terminals/banner-dismissed")

    assert response.status_code == 200
    assert response.get_json() == {"dismissed": False}


def test_terminal_banner_dismissal_round_trips(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted "never show again" is reflected on the next read."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    post_response = client.post("/api/terminals/banner-dismissed", json={"dismissed": True})
    assert post_response.status_code == 200
    assert post_response.get_json()["dismissed"] is True

    get_response = client.get("/api/terminals/banner-dismissed")
    assert get_response.get_json() == {"dismissed": True}


def test_destroy_terminal_refuses_agent_prefixed_session(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The destroy endpoint never kills an mngr agent's tmux session."""
    monkeypatch.setenv("MNGR_PREFIX", "mngr-")
    response = client.post("/api/terminals/mngr-alice/destroy")

    assert response.status_code == 400


def test_terminal_notify_rejects_unknown_kind(client: FlaskClient) -> None:
    """An unrecognized notify kind is a 400."""
    response = client.post("/api/terminals/notify", json={"kind": "bogus"})

    assert response.status_code == 400


def test_terminal_notify_session_renamed_broadcasts(client: FlaskClient) -> None:
    """A rename notification always broadcasts (matched by session_id downstream)."""
    response = client.post(
        "/api/terminals/notify",
        json={"kind": "session-renamed", "session_name": "terminal-2", "session_id": "$4"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "broadcast": True}


def test_terminal_notify_session_changed_skips_when_client_unresolved(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session switch from a client with no ttyd mapping does not broadcast."""
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    response = client.post(
        "/api/terminals/notify",
        json={
            "kind": "session-changed",
            "client_tty": "/dev/pts/9",
            "session_name": "terminal-1",
            "session_id": "$3",
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "broadcast": False}


def test_terminal_notify_session_changed_resolves_terminal_id_from_clients_map(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session switch broadcasts once the client tty maps to a known terminal tab."""
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))
    clients_dir = tmp_path / "commands" / "ttyd" / "clients"
    clients_dir.mkdir(parents=True)
    (clients_dir / "term-xyz").write_text("/dev/pts/7\n")

    response = client.post(
        "/api/terminals/notify",
        json={
            "kind": "session-changed",
            "client_tty": "/dev/pts/7",
            "session_name": "terminal-1",
            "session_id": "$3",
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "broadcast": True}


def test_index_injects_hostname_meta_tag(tmp_path: Path) -> None:
    """The index page includes a hostname meta tag."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><head></head><body>test</body></html>")

    with patch("imbue.system_interface.server.STATIC_DIRECTORY", static_dir):
        test_client = create_application(build_test_state()).test_client()
        response = test_client.get("/")
        assert response.status_code == 200
        assert "system-interface-hostname" in response.text


def test_random_name_endpoint(client: FlaskClient) -> None:
    """The random name endpoint returns a non-empty name."""
    response = client.get("/api/random-name")
    assert response.status_code == 200
    data = response.get_json()
    assert "name" in data
    assert len(data["name"]) > 0


def test_create_chat_agent_without_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a chat agent without a primary agent work dir returns 400."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    test_client = create_application(build_test_state()).test_client()
    response = test_client.post(
        "/api/agents/create-chat",
        json={"name": "test-chat"},
    )
    assert response.status_code == 400


def test_create_worktree_agent_missing_agent(client: FlaskClient) -> None:
    """Creating a worktree agent with an unknown selected agent returns 400."""
    response = client.post(
        "/api/agents/create-worktree",
        json={"name": "test-worktree", "selected_agent_id": "nonexistent"},
    )
    assert response.status_code == 400


@pytest.mark.timeout(15)
def test_websocket_endpoint_sends_initial_snapshot(app: Flask) -> None:
    """The WebSocket endpoint sends agents_updated and apps_updated on connect."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            msg1 = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            msg2 = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    types = {msg1["type"], msg2["type"]}
    assert "agents_updated" in types
    assert "apps_updated" in types


def _next_broadcast_message(client_queue: "queue.Queue[str | None]") -> dict[str, Any]:
    """Pop the next broadcast off a fake client's queue as a parsed object."""
    raw_message = client_queue.get_nowait()
    assert raw_message is not None
    parsed = json.loads(raw_message)
    assert isinstance(parsed, dict)
    return parsed


def _register_fake_client(app: Flask, client_id: str, layout_slug: str) -> "queue.Queue[str | None]":
    """Register a fake WS client on ``layout_slug`` and return its message queue.

    Deterministic stand-in for the real WebSocket registration path: broadcast
    messages land in the returned queue, so tests can assert targeted delivery
    without a live socket or timing.
    """
    broadcaster = state_of(app).broadcaster
    client_queue = broadcaster.register()
    broadcaster.set_client_info(client_queue, client_id, layout_slug, "desktop")
    return client_queue


def test_layout_broadcast_open_emits_targeted_ws_message(app: Flask) -> None:
    """op=open reaches exactly the clients whose active layout matches --layout."""
    matching_queue = _register_fake_client(app, "client-on-desktop", "desktop")
    other_queue = _register_fake_client(app, "client-on-mobile", "mobile")

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web", "layout": "desktop"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200

    msg = _next_broadcast_message(matching_queue)
    # The internal ``layout`` targeting arg is stripped before broadcast.
    assert msg == {
        "type": "layout_op",
        "op": "open",
        "args": {"ref": "service:web"},
        "requester_agent_id": "agent-42",
    }
    assert other_queue.empty()


def test_layout_broadcast_mutating_op_requires_layout(app: Flask) -> None:
    """A mutating op without a target layout is a 400."""
    _register_fake_client(app, "client-1", "desktop")
    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 400
    assert "requires a target layout" in response.get_json()["detail"]


def test_layout_broadcast_mutating_op_without_matching_client_is_412(app: Flask) -> None:
    """With no connected client on the target layout, the op fails loudly."""
    _register_fake_client(app, "client-1", "desktop")
    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web", "layout": "mobile"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 412
    assert "No connected client has layout" in response.get_json()["detail"]


def test_layout_broadcast_sessionless_browser_is_rejected(app: Flask) -> None:
    """A bare ``service:browser`` open (no ``?session=<name>``) is a 400 -- it would spawn
    the orphan session-less viewer pane. A session-qualified ref goes through."""
    matching_queue = _register_fake_client(app, "client-1", "desktop")
    client = app.test_client()
    # Bare browser ref -> rejected with a guiding message (fires before the layout checks).
    bare = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:browser", "layout": "desktop"}, "agent_id": "agent-42"},
    )
    assert bare.status_code == 400
    assert "needs a specific browser name" in bare.get_json()["detail"]
    # nothing should have been broadcast to the client
    assert matching_queue.empty()
    # A session-qualified browser ref is allowed and reaches the client.
    ok = client.post(
        "/api/layout/broadcast",
        json={
            "op": "open",
            "args": {"ref": "service:browser?session=alex-smith", "layout": "desktop"},
            "agent_id": "agent-42",
        },
    )
    assert ok.status_code == 200
    msg = _next_broadcast_message(matching_queue)
    assert msg["args"]["ref"] == "service:browser?session=alex-smith"


def test_layout_broadcast_mutating_op_unknown_layout_is_404(app: Flask) -> None:
    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web", "layout": "no-such-layout"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 404
    assert "known layouts" in response.get_json()["detail"]
    # Projects are addressable targets too, so the miss lists them as well.
    assert "known projects" in response.get_json()["detail"]


def test_layout_broadcast_mutating_op_targets_a_project(app: Flask) -> None:
    """``--layout <project name>`` reaches the clients that have that project active.

    A connected client reports its active *project* as its active layout (that
    project is the arrangement it autosaves into), so a name that is not one of
    the named layouts resolves through the projects registry instead.
    """
    matching_queue = _register_fake_client(app, "client-on-project-1", "project-1")
    other_queue = _register_fake_client(app, "client-on-desktop", "desktop")

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web", "layout": "Project 1"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200

    msg = _next_broadcast_message(matching_queue)
    assert msg == {
        "type": "layout_op",
        "op": "open",
        "args": {"ref": "service:web"},
        "requester_agent_id": "agent-42",
    }
    assert other_queue.empty()


def test_layout_broadcast_mutating_op_targets_everything(app: Flask) -> None:
    """``--layout Everything`` reaches a client sitting in the unfiltered view.

    Everything is a view rather than a project, so it has no registry entry to
    resolve against -- but it is the home, and a client is as likely to be in it
    as in any project. Naming it must therefore address that client instead of
    reporting an unknown layout.
    """
    matching_queue = _register_fake_client(app, "client-on-everything", EVERYTHING_VIEW_ID)
    other_queue = _register_fake_client(app, "client-on-project-1", "project-1")

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web", "layout": EVERYTHING_VIEW_NAME}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200

    msg = _next_broadcast_message(matching_queue)
    assert msg == {
        "type": "layout_op",
        "op": "open",
        "args": {"ref": "service:web"},
        "requester_agent_id": "agent-42",
    }
    assert other_queue.empty()


def _isolated_client_activity_events_path() -> Path:
    """The client-activity events file inside the autouse-isolated MNGR_HOST_DIR."""
    return (
        Path(os.environ["MNGR_HOST_DIR"])
        / "agents"
        / os.environ["MNGR_AGENT_ID"]
        / "workspace_layout"
        / "events"
        / "client_activity"
        / "events.jsonl"
    )


def test_ws_client_connected_write_uses_connect_time_layout_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WS connection writes client-activity to the dir captured at connect,
    not whatever ``MNGR_HOST_DIR`` points at when the message is processed.

    Guards against a cross-server leak: in tests several servers share one
    process's env, so a lingering connection that re-resolved the events path
    from live env at write time could append into a *different* server's
    activity log (observed as a flaky failure in the layout ``context`` test,
    which then saw a client it never created).
    """
    captured_layout_dir = tmp_path / "server_a" / "workspace_layout"
    # After connect, pretend the process env has moved on to a different server.
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "server_b"))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-b")

    broadcaster = WebSocketBroadcaster()
    client_queue = broadcaster.register()
    try:
        handled = _handle_client_state_message(
            json.dumps(
                {
                    "type": "client_state",
                    "client_id": "client-xyz",
                    "active_layout": "desktop",
                    "device_kind": "desktop",
                }
            ),
            client_queue,
            broadcaster,
            layout_dir=captured_layout_dir,
            is_first_report=True,
        )
    finally:
        broadcaster.unregister(client_queue)

    assert handled is True
    # The event landed in the connect-time dir...
    written = client_activity.read_client_activity_events(client_activity.get_events_path(captured_layout_dir))
    assert [event["client_id"] for event in written] == ["client-xyz"]
    # ...and nothing leaked into the (now-current) live-env server's location.
    live_env_layout_dir = (
        Path(os.environ["MNGR_HOST_DIR"]) / "agents" / os.environ["MNGR_AGENT_ID"] / "workspace_layout"
    )
    assert not client_activity.get_events_path(live_env_layout_dir).exists()


def test_layout_broadcast_load_targets_recent_messager(app: Flask) -> None:
    """``load`` resolves the requesting client from the message-event log."""
    events_path = _isolated_client_activity_events_path()
    client_activity.append_message_event(
        events_path,
        client_id="client-7",
        device_kind="mobile",
        layout_slug="desktop",
        agent_id="agent-42",
        agent_name="chat-agent",
        message_text="set up my mobile layout",
    )
    listener_queue = _register_fake_client(app, "client-7", "desktop")

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "load", "args": {"layout": "mobile"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["layout"] == "mobile"
    assert body["target_client_id"] == "client-7"

    msg = _next_broadcast_message(listener_queue)
    assert msg == {
        "type": "load_layout",
        "layout_slug": "mobile",
        "display_name": "mobile",
        "target_client_id": "client-7",
    }


def test_layout_broadcast_load_unknown_layout_is_404(app: Flask) -> None:
    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "load", "args": {"layout": "no-such-layout"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 404


def test_layout_broadcast_context_summarizes_clients(app: Flask) -> None:
    """``context`` folds the event log + live registry into per-client summaries."""
    events_path = _isolated_client_activity_events_path()
    client_activity.append_message_event(
        events_path,
        client_id="client-7",
        device_kind="mobile",
        layout_slug="desktop",
        agent_id="agent-42",
        agent_name="chat-agent",
        message_text="hello there",
    )
    # The live registry says the client has since switched to mobile; it
    # overrides the event-derived layout.
    _register_fake_client(app, "client-7", "mobile")

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "context", "args": {}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    clients = response.get_json()["clients"]
    assert len(clients) == 1
    summary = clients[0]
    assert summary["client_id"] == "client-7"
    assert summary["current_layout"] == "mobile"
    assert summary["is_connected"] is True
    assert summary["recent_messages"][0]["text"] == "hello there"


@pytest.mark.timeout(15)
def test_layout_broadcast_refresh_bypasses_mutex(app: Flask) -> None:
    """``refresh`` is read-only and never acquires the mutex."""
    client = app.test_client()
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))

            response = client.post(
                "/api/layout/broadcast",
                json={"op": "refresh", "args": {"ref": "service:web"}, "agent_id": "agent-42"},
            )
            assert response.status_code == 200
            msg = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert msg == {
        "type": "layout_op",
        "op": "refresh",
        "args": {"ref": "service:web"},
        "requester_agent_id": "agent-42",
    }


@pytest.mark.timeout(15)
def test_layout_broadcast_reload_system_interface_emits_ws_message(app: Flask) -> None:
    """``reload_system_interface`` broadcasts a layout_op so the shell reloads.

    This is the frontend-reveal trigger: the reload script POSTs this op and the
    dockview shell responds by reloading the whole top-level page. It carries no
    args and bypasses the mutex (read-only).
    """
    client = app.test_client()
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))

            response = client.post(
                "/api/layout/broadcast",
                json={"op": "reload_system_interface", "args": {}, "agent_id": "agent-42"},
            )
            assert response.status_code == 200
            msg = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert msg == {
        "type": "layout_op",
        "op": "reload_system_interface",
        "args": {},
        "requester_agent_id": "agent-42",
    }


def test_layout_broadcast_open_terminal_allocates_panel_id_and_returns_ref(app: Flask) -> None:
    """``open service:terminal`` is the synchronous-ref-return path.

    The endpoint pre-mints the panel id (so the frontend uses it
    verbatim and the resulting tab is deterministically addressable as
    ``terminal:<hash>``), injects it into the broadcast args, and
    returns the ref in the HTTP response. Every other op leaves the
    args dict alone and returns just ``{ok: true}``.
    """
    listener_queue = _register_fake_client(app, "client-1", "desktop")
    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:terminal", "layout": "desktop"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    body = response.get_json()
    ref = body["ref"]
    assert ref.startswith("terminal:")

    msg = _next_broadcast_message(listener_queue)
    assert msg["op"] == "open"
    assert msg["requester_agent_id"] == "agent-42"
    # The frontend must receive the same panel id the server returned
    # the ref for, or the script's printed ref would address nothing.
    panel_id = msg["args"]["panel_id"]
    assert panel_id.startswith("iframe-terminal-")
    assert msg["args"]["ref"] == "service:terminal"


def test_layout_broadcast_open_non_terminal_returns_no_ref(app: Flask) -> None:
    """Non-terminal opens must NOT carry a ``ref`` in the response: the
    CLI uses presence-of-ref to decide whether to print to stdout, and a
    stray ref on a regular service open would mislead callers."""
    _register_fake_client(app, "client-1", "desktop")
    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web", "layout": "desktop"}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    assert "ref" not in response.get_json()


@pytest.mark.timeout(15)
def test_ws_client_state_registration_enables_targeted_ops(app: Flask) -> None:
    """A real client_state message over the WebSocket registers the client.

    Exercises the receive-poll path in the broadcast loop end to end: before
    registration a desktop-targeted op is a 412; after the loop processes the
    registration (at most one queue-poll later) the same op succeeds and the
    layout_op arrives on this socket.
    """
    client = app.test_client()
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            # Drain the initial snapshot messages.
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))

            ws.send(
                json.dumps(
                    {
                        "type": "client_state",
                        "client_id": "client-9",
                        "active_layout": "desktop",
                        "device_kind": "desktop",
                    }
                )
            )
            # Retry the op until the registration lands, using the socket's
            # own receive timeout as the pacing delay (no time.sleep).
            deadline = time.monotonic() + 10.0
            status_code = 0
            while time.monotonic() < deadline:
                response = client.post(
                    "/api/layout/broadcast",
                    json={
                        "op": "focus",
                        "args": {"ref": "chat:someone", "layout": "desktop"},
                        "agent_id": "agent-42",
                    },
                )
                status_code = response.status_code
                if status_code == 200:
                    break
                assert status_code == 412
                ws.receive(timeout=0.05)
            assert status_code == 200

            msg = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert msg["type"] == "layout_op"
    assert msg["op"] == "focus"
    assert msg["args"] == {"ref": "chat:someone"}


def test_layout_broadcast_rejects_non_loopback(client: FlaskClient) -> None:
    """The layout broadcast endpoint refuses non-loopback callers."""
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": {"ref": "service:web"}, "agent_id": "agent-42"},
        environ_base={"REMOTE_ADDR": "10.0.0.1"},
    )
    assert response.status_code == 403


def test_get_events_seeds_pending_tool_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hitting /api/agents/{id}/events for a Claude session with an unmatched tool_use
    seeds the AgentManager's transcript-derived signals so the activity indicator
    reads ``TOOL_RUNNING`` immediately.
    """
    agent_id = "agent-pending-tool"
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", agent_id)
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path / "work"))

    state_dir = tmp_path / "agents" / agent_id
    state_dir.mkdir(parents=True)

    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)
    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    # An assistant message that includes a tool_use, with no matching tool_result.
    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [
                        {"type": "text", "text": "running a command"},
                        {"type": "tool_use", "id": "call_a", "name": "Bash", "input": {"command": "ls"}},
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        + "\n"
    )
    (state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    broadcaster = WebSocketBroadcaster()
    manager = AgentManager.build(broadcaster)
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name="seed-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path / "work"),
        )
    manager._ensure_activity_tracking(agent_id)

    app = create_application(build_test_state(agent_manager=manager))
    agent_info = AgentInfo(
        id=agent_id,
        name="seed-agent",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=claude_config_dir,
    )

    try:
        test_client = app.test_client()
        with patch("imbue.system_interface.server._find_agent", return_value=agent_info):
            response = test_client.get(f"/api/agents/{agent_id}/events")
        assert response.status_code == 200

        # The watcher creation path seeds transcript-derived state
        # synchronously. Assert before ``stop()``, which clears these
        # caches alongside the marker watchers.
        with manager._lock:
            assert manager._has_unmatched_tool_use_by_agent[agent_id] is True
            assert manager._activity_state_by_agent[agent_id] == ActivityState.TOOL_RUNNING
    finally:
        manager.stop()


def test_layout_broadcast_rejects_unknown_op(client: FlaskClient) -> None:
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "explode", "args": {}, "agent_id": "agent-42"},
    )
    assert response.status_code == 400
    assert "Unknown layout op" in response.get_json()["detail"]


def test_layout_broadcast_rejects_non_dict_args(client: FlaskClient) -> None:
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "open", "args": ["not", "a", "dict"], "agent_id": "agent-42"},
    )
    assert response.status_code == 400


def test_layout_broadcast_rejects_null_args(client: FlaskClient) -> None:
    """``args: null`` must be a 400, not silently coerced into ``{}``.

    A previous implementation collapsed every falsy non-dict via ``or {}``,
    which let mutating ops broadcast empty payloads that the frontend
    handlers silently dropped.
    """
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "close", "args": None, "agent_id": "agent-42"},
    )
    assert response.status_code == 400


def test_layout_broadcast_mutex_returns_409_with_holder_metadata(app: Flask) -> None:
    """While agent A holds the mutex, agent B's mutating op is rejected with 409."""
    # Pre-acquire the mutex on behalf of agent-a so the test's request
    # races against an active holder deterministically (no thread timing).
    mutex: LayoutMutex = state_of(app).layout_mutex
    held = mutex.try_acquire("agent-a", "move", {"ref": "service:web"})
    assert held is None

    _register_fake_client(app, "client-1", "desktop")
    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "split", "args": {"ref": "service:api", "layout": "desktop"}, "agent_id": "agent-b"},
    )
    assert response.status_code == 409
    body = response.get_json()
    assert body["retry_after_ms"] > 0
    in_flight = body["in_flight"]
    assert in_flight["agent_id"] == "agent-a"
    assert in_flight["operation"] == "move"
    assert in_flight["args"] == {"ref": "service:web"}


def test_layout_broadcast_inspect_reads_layout_json(
    app: Flask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``inspect`` returns a ref-resolved summary of the saved layout."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-42")
    layout_dir = tmp_path / "agents" / "agent-42" / "workspace_layout"
    layout_dir.mkdir(parents=True)
    (layout_dir / "layout.json").write_text(
        json.dumps(
            {
                "dockview": {
                    "panels": {
                        "panel-1": {"id": "panel-1", "title": "web"},
                        "panel-2": {"id": "panel-2", "title": "chat"},
                    },
                    "grid": {
                        "root": {
                            "type": "leaf",
                            "data": {"views": ["panel-1", "panel-2"], "activeView": "panel-1", "size": 1.0},
                        },
                    },
                },
                "panelParams": {
                    "panel-1": {"panelType": "iframe", "serviceName": "web"},
                    "panel-2": {"panelType": "chat", "chatAgentId": "agent-42"},
                },
            }
        )
    )

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "inspect", "args": {}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    layout_summary = payload["layout"]
    refs = [p["ref"] for p in layout_summary["panels"]]
    assert "service:web" in refs


def test_layout_broadcast_list_includes_open_flag(app: Flask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``list`` reads the saved layout to compute ``is_open`` per service."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-42")
    layout_dir = tmp_path / "agents" / "agent-42" / "workspace_layout"
    layout_dir.mkdir(parents=True)
    (layout_dir / "layout.json").write_text(
        json.dumps(
            {
                "dockview": {"panels": {"panel-1": {"id": "panel-1", "title": "web"}}},
                "panelParams": {"panel-1": {"panelType": "iframe", "serviceName": "web"}},
            }
        )
    )

    client = app.test_client()
    response = client.post(
        "/api/layout/broadcast",
        json={"op": "list", "args": {}, "agent_id": "agent-42"},
    )
    assert response.status_code == 200
    entries = response.get_json()["entries"]
    # We don't know what services the agent_manager seeded; assert the
    # endpoint shape and that ``is_open`` is bool-typed if any entry exists.
    for entry in entries:
        assert set(entry.keys()) == {"ref", "kind", "display_name", "is_open", "is_running"}
        assert isinstance(entry["is_open"], bool)


@pytest.mark.timeout(15)
def test_proto_agent_logs_endpoint_not_found_sends_error_and_closes(app: Flask) -> None:
    """When the proto-agent is missing, the endpoint sends a structured not-found message and closes."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/proto-agents/missing-agent/logs")
        try:
            payload = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)
    assert payload == {"done": True, "success": False, "error": "Proto-agent not found"}


@pytest.mark.timeout(15)
def test_proto_agent_logs_endpoint_streams_messages_until_sentinel(app: Flask) -> None:
    """The endpoint forwards real log lines and closes when the queue yields ``None``."""
    log_queue: queue.Queue[str | None] = queue.Queue()
    log_queue.put(json.dumps({"line": "starting"}))
    log_queue.put(json.dumps({"line": "still going"}))
    log_queue.put(None)

    agent_manager: AgentManager = state_of(app).agent_manager
    agent_manager._log_queues["proto-1"] = log_queue

    with serve_app(app) as served:
        ws = open_ws(served, "/api/proto-agents/proto-1/logs")
        try:
            first = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            second = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert first == {"line": "starting"}
    assert second == {"line": "still going"}


def test_stream_filtered_events_forwards_only_matching_events() -> None:
    """The shared stream loop yields only events that pass its predicate.

    This is the wiring behind Bug 2: the main stream forwards main-session
    events and drops subagent-session events, which share the same per-agent
    queue. A queued ``None`` ends the stream, keeping the test deterministic.
    """
    event_queues = AgentEventQueues()
    event_queue = event_queues.register("agent-1")

    # Subagent event first so a missing filter would forward it before the main one.
    event_queue.put({"event_id": "sub-evt", "session_id": "agent-sub"})
    event_queue.put({"event_id": "main-evt", "session_id": "main-1"})
    # Plugin/app events have no session_id and must still pass through.
    event_queue.put({"event_id": "no-session"})
    event_queue.put(None)

    def is_main_session_event(event: dict[str, object]) -> bool:
        session_id = event.get("session_id")
        return session_id is None or session_id == "main-1"

    frames = list(_stream_filtered_events("agent-1", event_queues, event_queue, is_main_session_event))
    forwarded_ids = [json.loads(frame[len("data: ") :])["event_id"] for frame in frames if frame.startswith("data: ")]

    assert forwarded_ids == ["main-evt", "no-session"]
    assert "sub-evt" not in forwarded_ids


def test_destroy_rejects_is_primary_agent(client: FlaskClient, app: Flask) -> None:
    """POST /api/agents/<id>/destroy returns 400 for the services agent.

    The frontend already hides agents carrying ``is_primary=true``; this
    server-side guard prevents direct callers (curl, scripted use, etc.)
    from accidentally tearing down the workspace.
    """
    agent_manager: AgentManager = state_of(app).agent_manager
    services_agent = AgentStateItem(
        id="services-1",
        name="system-services",
        state="RUNNING",
        labels={"is_primary": "true", "workspace": "my-ws"},
        work_dir="/home/user/workspace",
    )
    agent_manager._agents[services_agent.id] = services_agent

    response = client.post(f"/api/agents/{services_agent.id}/destroy")
    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    # The guard runs *before* the destroy subprocess, so the agent is still
    # present in the agent manager's state.
    assert services_agent.id in agent_manager._agents


def _register_agent(app: Flask, agent_id: str, name: str, state: str) -> None:
    """Insert an agent into the AgentManager's state for endpoint tests."""
    agent_manager: AgentManager = state_of(app).agent_manager
    agent_manager._agents[agent_id] = AgentStateItem(
        id=agent_id,
        name=name,
        state=state,
        labels={},
        work_dir="/code",
    )


def test_start_unknown_agent_returns_404(client: FlaskClient) -> None:
    """POST /api/agents/<id>/start returns 404 for an unknown agent."""
    response = client.post("/api/agents/nonexistent/start")
    assert response.status_code == 404


def test_start_invokes_in_process_start_with_agent_name(client: FlaskClient, app: Flask) -> None:
    """The endpoint delegates to the in-process ``start_agent`` keyed by name.

    Opening a terminal must go through the same in-process mngr start path that
    messaging an agent uses, so the two cannot diverge. The endpoint therefore
    calls ``start_agent(<name>)`` rather than shelling out to ``mngr start``.
    """
    _register_agent(app, "agent-running", "running-agent", "RUNNING")

    with patch("imbue.system_interface.server.start_agent") as mock_start:
        response = client.post("/api/agents/agent-running/start")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    mock_start.assert_called_once_with("running-agent")


def test_start_failure_returns_500(client: FlaskClient, app: Flask) -> None:
    """A failed start surfaces as a 500 carrying the mngr error message."""
    _register_agent(app, "agent-stopped", "stopped-agent", "STOPPED")

    with patch(
        "imbue.system_interface.server.start_agent",
        side_effect=AgentStartError("stopped-agent", "boom"),
    ):
        response = client.post("/api/agents/agent-stopped/start")

    assert response.status_code == 500
    assert "boom" in response.get_json()["detail"]


def test_destroy_argv_accepted_by_live_cli() -> None:
    """Confront the ``mngr destroy`` argv with the live ``imbue.mngr.main.cli``
    tree, so a system/vendor/mngr rename of that subcommand/flag fails here at merge
    time rather than only surfacing at runtime."""
    assert_mngr_argv_valid(_build_destroy_command("demo"))


# -- Agent file serving (markdown images + download links) --------------------
#
# An agent writes a file and references its absolute on-disk path in markdown;
# the catch-all serves that file -- images inline so they render, any other file
# as a download. These exercise the catch-all dispatch end to end via the Flask
# test client.


def test_serves_image_at_its_absolute_path(client: FlaskClient, tmp_path: Path) -> None:
    """A request for an existing image file's absolute path streams its bytes inline."""
    image_path = tmp_path / "chart.png"
    image_bytes = b"fake-png-bytes"
    image_path.write_bytes(image_bytes)

    response = client.get(str(image_path))

    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == image_bytes
    # Inline (rendered), not a forced download.
    assert "attachment" not in response.headers.get("Content-Disposition", "")
    # Cached aggressively: filenames are unique per image by convention.
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_ignores_requested_at_cache_busting_query(client: FlaskClient, tmp_path: Path) -> None:
    """The frontend's per-message ``?requested_at=`` cache key is ignored server-side.

    The query string never reaches ``try_serve_file`` (Flask splits it off before
    routing), so a request carrying it serves the same file with the same headers
    as the bare path. It exists only to make the browser treat each message's URL
    as distinct so a new message never renders a stale cached copy.
    """
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"fake-png-bytes")

    tagged = client.get(f"{image_path}?requested_at=2026-07-24T00%3A00%3A00Z")

    assert tagged.status_code == 200
    assert tagged.content_type == "image/png"
    assert tagged.data == b"fake-png-bytes"
    assert tagged.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serves_image_in_nested_subdirectory(client: FlaskClient, tmp_path: Path) -> None:
    """Nested paths under the write directory are served (agents may organize per run)."""
    nested_dir = tmp_path / "images" / "run-3"
    nested_dir.mkdir(parents=True)
    image_path = nested_dir / "diagram.webp"
    image_path.write_bytes(b"fake-webp-bytes")

    response = client.get(str(image_path))

    assert response.status_code == 200
    assert response.content_type == "image/webp"


def test_serves_image_with_uppercase_extension(client: FlaskClient, tmp_path: Path) -> None:
    """Image extensions are matched case-insensitively."""
    image_path = tmp_path / "SHOT.PNG"
    image_path.write_bytes(b"fake-png-bytes")

    response = client.get(str(image_path))

    assert response.status_code == 200
    assert response.content_type == "image/png"


def test_serves_svg_with_hardened_headers(client: FlaskClient, tmp_path: Path) -> None:
    """SVG is served as an image but locked down for direct navigation."""
    image_path = tmp_path / "plot.svg"
    image_path.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")

    response = client.get(str(image_path))

    assert response.status_code == 200
    # Werkzeug appends "; charset=utf-8" to the XML-based svg type; harmless.
    assert response.content_type.startswith("image/svg+xml")
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_missing_image_path_returns_404_not_app_shell(client: FlaskClient, tmp_path: Path) -> None:
    """A typo'd image path renders a broken image (404), never the SPA shell."""
    missing_path = tmp_path / "nope.png"

    response = client.get(str(missing_path))

    assert response.status_code == 404


def test_directory_with_image_extension_returns_404(client: FlaskClient, tmp_path: Path) -> None:
    """A directory whose name ends in an image extension is not a servable file."""
    directory = tmp_path / "weird.png"
    directory.mkdir()

    response = client.get(str(directory))

    assert response.status_code == 404


def test_nonexistent_path_falls_through_to_app_shell(client: FlaskClient, tmp_path: Path) -> None:
    """A path matching no file is a client-side route: it returns the app shell, not a 404.

    Only paths that resolve to a real file are served; everything else falls
    through so the single-page-app's client-side routing keeps working.
    """
    response = client.get(str(tmp_path / "some" / "client" / "route"))

    assert response.status_code == 200
    assert "text/html" in response.content_type


def test_serves_image_with_spaces_in_filename(client: FlaskClient, tmp_path: Path) -> None:
    """A descriptive filename with spaces (percent-encoded in the URL) still serves.

    The whole feature relies on the framework percent-decoding the catch-all path
    before the handler reconstructs the on-disk path; pin that for a filename an
    agent told to use 'descriptive' names could realistically produce.
    """
    image_path = tmp_path / "my chart 2026.png"
    image_bytes = b"fake-png-bytes"
    image_path.write_bytes(image_bytes)

    response = client.get(quote(str(image_path)))

    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == image_bytes


def test_serves_image_with_unicode_filename(client: FlaskClient, tmp_path: Path) -> None:
    """A non-ASCII filename (percent-encoded in the URL) serves the right bytes."""
    image_path = tmp_path / "gráfico.png"
    image_bytes = b"fake-png-bytes"
    image_path.write_bytes(image_bytes)

    response = client.get(quote(str(image_path)))

    assert response.status_code == 200
    assert response.data == image_bytes


def test_serves_non_image_file_as_download(client: FlaskClient, tmp_path: Path) -> None:
    """A non-image file is served as an attachment (download), not rendered inline."""
    file_path = tmp_path / "q4-report.pdf"
    file_bytes = b"%PDF-1.4 fake-pdf-bytes"
    file_path.write_bytes(file_bytes)

    response = client.get(str(file_path))

    assert response.status_code == 200
    assert response.data == file_bytes
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment" in disposition
    assert "q4-report.pdf" in disposition
    # Downloaded, not sniffed into an inline-executable type.
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    # Cached forever like inline images; per-message ``requested_at`` keeps a new
    # message's link URL distinct so it still fetches the current file.
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serves_extensionless_file_as_download(client: FlaskClient, tmp_path: Path) -> None:
    """A file with no extension is still served as a download when it exists."""
    file_path = tmp_path / "server-log"
    file_bytes = b"line one\nline two\n"
    file_path.write_bytes(file_bytes)

    response = client.get(str(file_path))

    assert response.status_code == 200
    assert response.data == file_bytes
    assert "attachment" in response.headers.get("Content-Disposition", "")


def test_missing_non_image_path_is_not_a_download(client: FlaskClient, tmp_path: Path) -> None:
    """A non-image path with no file behind it falls through to the app shell, not a download."""
    response = client.get(str(tmp_path / "does-not-exist.pdf"))

    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert "attachment" not in response.headers.get("Content-Disposition", "")


def _build_stub_browser_backend() -> Flask:
    """A tiny stand-in for the browser daemon's fleet API.

    ``GET /browsers`` returns a fixed fleet listing; ``POST /browsers``
    echoes the submitted name on success and rejects the reserved name
    ``taken`` with the daemon's 409 error shape; ``DELETE /browsers/<name>``
    retires the browser, echoing the name on success and rejecting the
    reserved name ``missing`` with a 404, so tests can observe both the body
    forwarding and the status relay through the passthrough.
    """
    stub = Flask(__name__, static_folder=None)

    def list_browsers() -> Response:
        body = json.dumps({"browsers": [{"name": "main", "controller": None}]})
        return Response(body, mimetype="application/json")

    def create_browser() -> Response:
        payload = json.loads(flask_request.get_data() or b"{}")
        name = payload.get("name", "")
        if name == "taken":
            return Response(json.dumps({"error": "name already in use"}), status=409, mimetype="application/json")
        return Response(json.dumps({"name": name}), status=200, mimetype="application/json")

    def delete_browser(browser_id: str) -> Response:
        if browser_id == "missing":
            return Response(json.dumps({"error": "no such browser"}), status=404, mimetype="application/json")
        return Response(json.dumps({"closed": browser_id}), status=200, mimetype="application/json")

    stub.add_url_rule("/browsers", view_func=list_browsers, methods=["GET"])
    stub.add_url_rule("/browsers", view_func=create_browser, methods=["POST"], endpoint="create_browser")
    stub.add_url_rule("/browsers/<string:browser_id>", view_func=delete_browser, methods=["DELETE"])
    return stub


def _client_with_browser_service(url: str | None) -> FlaskClient:
    """Build a workspace app test client whose ``browser`` service points at ``url``.

    ``None`` leaves the apps registry empty (browser service not registered).
    """
    agent_manager = AgentManager.build(WebSocketBroadcaster())
    agent_manager._apps = [AppEntry(name="browser", url=url)] if url is not None else []
    return create_application(build_test_state(agent_manager=agent_manager)).test_client()


def test_get_browsers_passthrough_relays_backend_fleet() -> None:
    """``GET /api/browsers`` forwards to the browser daemon and relays its JSON."""
    with serve_app(_build_stub_browser_backend()) as backend:
        test_client = _client_with_browser_service(backend.http_url)
        response = test_client.get("/api/browsers")
        assert response.status_code == 200
        assert response.get_json() == {"browsers": [{"name": "main", "controller": None}]}


def test_post_browsers_passthrough_forwards_body_and_relays_success() -> None:
    """``POST /api/browsers`` forwards the JSON body and relays the daemon's response."""
    with serve_app(_build_stub_browser_backend()) as backend:
        test_client = _client_with_browser_service(backend.http_url)
        response = test_client.post("/api/browsers", json={"name": "research"})
        assert response.status_code == 200
        assert response.get_json() == {"name": "research"}


def test_post_browsers_passthrough_relays_backend_rejection() -> None:
    """A daemon rejection (409 + error body) passes through status and body verbatim."""
    with serve_app(_build_stub_browser_backend()) as backend:
        test_client = _client_with_browser_service(backend.http_url)
        response = test_client.post("/api/browsers", json={"name": "taken"})
        assert response.status_code == 409
        assert response.get_json() == {"error": "name already in use"}


def test_delete_browser_passthrough_forwards_and_relays_success() -> None:
    """``DELETE /api/browsers/<name>`` forwards to the daemon and relays its success."""
    with serve_app(_build_stub_browser_backend()) as backend:
        test_client = _client_with_browser_service(backend.http_url)
        response = test_client.delete("/api/browsers/research")
        assert response.status_code == 200
        assert response.get_json() == {"closed": "research"}


def test_delete_browser_passthrough_relays_backend_rejection() -> None:
    """A daemon 404 (unknown browser) passes through status and body verbatim."""
    with serve_app(_build_stub_browser_backend()) as backend:
        test_client = _client_with_browser_service(backend.http_url)
        response = test_client.delete("/api/browsers/missing")
        assert response.status_code == 404
        assert response.get_json() == {"error": "no such browser"}


def test_browsers_passthrough_returns_503_when_service_not_registered() -> None:
    """Without a registered ``browser`` service, every method returns a 503 JSON error."""
    test_client = _client_with_browser_service(None)
    for response in (
        test_client.get("/api/browsers"),
        test_client.post("/api/browsers", json={"name": "x"}),
        test_client.delete("/api/browsers/x"),
    ):
        assert response.status_code == 503
        assert "not registered" in response.get_json()["detail"]


def test_browsers_passthrough_returns_503_when_backend_is_unreachable() -> None:
    """A registered but dead backend surfaces as a 503 JSON error, not a raised exception."""
    test_client = _client_with_browser_service("http://127.0.0.1:1")
    response = test_client.get("/api/browsers")
    assert response.status_code == 503
    assert "unreachable" in response.get_json()["detail"]


def test_list_projects_seeds_one_starter_project(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh workspace lists one starter project: empty, and already the active one.

    "Everything" is the unfiltered view rather than a project, so it is
    deliberately absent from the registry even though it keeps a layout.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/projects")

    assert response.status_code == 200
    body = response.get_json()
    assert [project["project_id"] for project in body["projects"]] == ["project-1"]
    assert body["projects"][0]["name"] == "Project 1"
    assert body["projects"][0]["has_content"] is False
    assert body["projects"][0]["members"] == []
    assert body["last_active_id"] == "project-1"


def test_create_project_slugifies_and_registers(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create returns the new project and appends it to the registry."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    response = client.post("/api/projects", json={"name": "Data Pipeline", "color": "#3B82F6", "glyph": 6})

    assert response.status_code == 200
    assert response.get_json() == {
        "project_id": "data-pipeline",
        "name": "Data Pipeline",
        "color": "#3B82F6",
        "glyph": 6,
        "has_content": False,
        "members": [],
    }
    list_response = client.get("/api/projects")
    assert [project["project_id"] for project in list_response.get_json()["projects"]] == [
        "project-1",
        "data-pipeline",
    ]


def test_create_project_rejects_conflicts_and_bad_metadata(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slug collision is a 409; an unusable name, color, or glyph is a 400."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    first = client.post("/api/projects", json={"name": "My Work", "color": "#3B82F6", "glyph": 1})
    assert first.status_code == 200
    conflict = client.post("/api/projects", json={"name": "my work", "color": "#3B82F6", "glyph": 2})
    assert conflict.status_code == 409
    assert "conflicts" in conflict.get_json()["detail"]

    unusable_name = client.post("/api/projects", json={"name": "!!!", "color": "#3B82F6", "glyph": 1})
    assert unusable_name.status_code == 400
    bad_color = client.post("/api/projects", json={"name": "Fine", "color": "blue", "glyph": 1})
    assert bad_color.status_code == 400
    out_of_range_glyph = client.post("/api/projects", json={"name": "Fine", "color": "#3B82F6", "glyph": 10})
    assert out_of_range_glyph.status_code == 400
    missing_glyph = client.post("/api/projects", json={"name": "Fine", "color": "#3B82F6"})
    assert missing_glyph.status_code == 400


def test_get_empty_project_returns_null_content(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered-but-never-saved project reports null content (fresh state)."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/projects/project-1")

    assert response.status_code == 200
    assert response.get_json() == {"layout": None}


def test_get_unknown_project_returns_404(client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.get("/api/projects/nonexistent")

    assert response.status_code == 404


def test_autosave_and_get_project_round_trips(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    layout_data = {"dockview": {"panels": {}}, "panelParams": {"chat-1": {"panelType": "chat"}}}
    save_response = client.post("/api/projects/project-1", json={"layout": layout_data, "client_id": "client-1"})
    assert save_response.status_code == 200
    assert save_response.get_json()["status"] == "ok"

    get_response = client.get("/api/projects/project-1")
    assert get_response.status_code == 200
    assert get_response.get_json()["layout"] == layout_data
    assert (tmp_path / "agents" / "agent-123" / "workspace_layout" / "projects" / "project-1.json").exists()


def test_autosave_unknown_project_returns_404(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An autosave against a just-deleted project must not resurrect it."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    response = client.post("/api/projects/gone", json={"layout": {}, "client_id": "client-1"})

    assert response.status_code == 404


def test_update_project_settings_keeps_id_content_and_members(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename changes only the display metadata; id, content and members survive."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects", json={"name": "Alpha", "color": "#3B82F6", "glyph": 2}).status_code == 200
    layout_data = {"dockview": {"panels": {}}, "panelParams": {}}
    assert client.post("/api/projects/alpha", json={"layout": layout_data, "client_id": "c1"}).status_code == 200
    assert client.post("/api/projects/alpha/members", json={"ref": "terminal:terminal-1"}).status_code == 200

    response = client.post(
        "/api/projects/alpha/settings", json={"name": "Renamed Alpha", "color": "#F0603A", "glyph": 7}
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "project_id": "alpha",
        "name": "Renamed Alpha",
        "color": "#F0603A",
        "glyph": 7,
        "has_content": True,
        "members": ["terminal:terminal-1"],
    }
    assert client.get("/api/projects/alpha").get_json()["layout"] == layout_data
    unknown = client.post("/api/projects/gone/settings", json={"name": "Gone", "color": "#F0603A", "glyph": 0})
    assert unknown.status_code == 404


def test_delete_project_reports_the_fallback_and_guards_the_last_one(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting reports the fallback project; the last remaining one is protected."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects", json={"name": "Scratch", "color": "#3B82F6", "glyph": 3}).status_code == 200

    delete_scratch = client.post("/api/projects/scratch/delete")
    assert delete_scratch.status_code == 200
    assert delete_scratch.get_json() == {
        "fallback_id": "project-1",
        "stopped": [],
        "failed": [],
        "left_running": [],
    }

    delete_last = client.post("/api/projects/project-1/delete")
    assert delete_last.status_code == 409

    delete_unknown = client.post("/api/projects/scratch/delete")
    assert delete_unknown.status_code == 404


def test_delete_project_stops_its_terminals_and_browsers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete tears down the members that have a stop verb and reports the rest.

    A terminal's tmux session is killed and a fleet browser is retired through
    the browser daemon -- the same teardown their own destroy endpoints use. A
    chat is an agent and an app is supervised elsewhere, so both are reported as
    still running rather than being killed off the back of a project delete.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    monkeypatch.setenv("MNGR_PREFIX", "mngr-")
    killed_session = FinishedProcess(
        returncode=0,
        stdout="",
        stderr="",
        command=("tmux", "kill-session", "-t", "=terminal-4"),
        is_output_already_logged=False,
    )
    with serve_app(_build_stub_browser_backend()) as backend:
        test_client = _client_with_browser_service(backend.http_url)
        assert (
            test_client.post("/api/projects", json={"name": "Scratch", "color": "#3B82F6", "glyph": 3}).status_code
            == 200
        )
        for ref in ("terminal:terminal-4", "service:browser?session=research", "chat:agent-9", "service:web"):
            assert test_client.post("/api/projects/scratch/members", json={"ref": ref}).status_code == 200
        with patch(
            "imbue.system_interface.server.run_local_command_modern_version", return_value=killed_session
        ) as mock_run:
            response = test_client.post("/api/projects/scratch/delete")

    assert response.status_code == 200
    body = response.get_json()
    assert body["stopped"] == ["terminal:terminal-4", "service:browser?session=research"]
    assert body["failed"] == []
    assert body["left_running"] == ["chat:agent-9", "service:web"]
    assert body["fallback_id"] == "project-1"
    assert mock_run.call_args.kwargs["command"] == ["tmux", "kill-session", "-t", "=terminal-4"]


def test_delete_project_reports_a_terminal_it_could_not_stop(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tmux session that survives the kill is reported as failed, not as stopped."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    monkeypatch.setenv("MNGR_PREFIX", "mngr-")
    failed_kill = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="can't kill session",
        command=("tmux", "kill-session", "-t", "=terminal-4"),
        is_output_already_logged=False,
    )
    still_listed = FinishedProcess(
        returncode=0,
        stdout="terminal-4\t$1\t/work\n",
        stderr="",
        command=("tmux", "list-sessions"),
        is_output_already_logged=False,
    )
    assert client.post("/api/projects", json={"name": "Scratch", "color": "#3B82F6", "glyph": 3}).status_code == 200
    assert client.post("/api/projects/scratch/members", json={"ref": "terminal:terminal-4"}).status_code == 200

    with patch(
        "imbue.system_interface.server.run_local_command_modern_version",
        side_effect=[failed_kill, still_listed],
    ):
        response = client.post("/api/projects/scratch/delete")

    assert response.status_code == 200
    assert response.get_json()["failed"] == ["terminal:terminal-4"]
    assert response.get_json()["stopped"] == []


def test_delete_project_reports_a_browser_it_could_not_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no browser daemon registered, the browser is reported as still running."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    test_client = _client_with_browser_service(None)
    assert (
        test_client.post("/api/projects", json={"name": "Scratch", "color": "#3B82F6", "glyph": 3}).status_code == 200
    )
    assert (
        test_client.post("/api/projects/scratch/members", json={"ref": "service:browser?session=research"}).status_code
        == 200
    )

    response = test_client.post("/api/projects/scratch/delete")

    assert response.status_code == 200
    assert response.get_json()["failed"] == ["service:browser?session=research"]
    assert response.get_json()["stopped"] == []


def test_delete_project_never_stops_an_agent_tmux_session(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``terminal:`` ref naming an mngr agent session is refused, not killed."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    monkeypatch.setenv("MNGR_PREFIX", "mngr-")
    assert client.post("/api/projects", json={"name": "Scratch", "color": "#3B82F6", "glyph": 3}).status_code == 200
    assert client.post("/api/projects/scratch/members", json={"ref": "terminal:mngr-alice"}).status_code == 200

    with patch("imbue.system_interface.server.run_local_command_modern_version") as mock_run:
        response = client.post("/api/projects/scratch/delete")

    assert response.status_code == 200
    assert response.get_json()["failed"] == ["terminal:mngr-alice"]
    mock_run.assert_not_called()


def test_add_member_files_a_ref_and_lists_it(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member is durable and independent of the layout: adding one lists it."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    response = client.post("/api/projects/project-1/members", json={"ref": "terminal:terminal-1"})

    assert response.status_code == 200
    assert response.get_json() == {"project_id": "project-1", "members": ["terminal:terminal-1"]}
    # Idempotent: re-adding what the project already owns is not an error.
    assert client.post("/api/projects/project-1/members", json={"ref": "terminal:terminal-1"}).status_code == 200
    listed = client.get("/api/projects").get_json()["projects"]
    assert listed[0]["members"] == ["terminal:terminal-1"]


def test_add_member_rejects_bad_bodies_and_unknown_projects(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank ref is a 400 and an unregistered project is a 404."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    blank_ref = client.post("/api/projects/project-1/members", json={"ref": "  "})
    assert blank_ref.status_code == 400
    missing_ref = client.post("/api/projects/project-1/members", json={})
    assert missing_ref.status_code == 400
    unknown_project = client.post("/api/projects/gone/members", json={"ref": "terminal:terminal-1"})
    assert unknown_project.status_code == 404


def test_add_member_files_a_ref_another_project_already_shows(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project is a view, so the same app can sit in as many as you like."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects", json={"name": "Alpha", "color": "#3B82F6", "glyph": 2}).status_code == 200
    assert client.post("/api/projects/project-1/members", json={"ref": "service:web"}).status_code == 200

    response = client.post("/api/projects/alpha/members", json={"ref": "service:web"})

    assert response.status_code == 200
    members_by_id = {
        project["project_id"]: project["members"] for project in client.get("/api/projects").get_json()["projects"]
    }
    assert members_by_id == {"project-1": ["service:web"], "alpha": ["service:web"]}


def test_remove_member_unfiles_it_without_touching_the_object(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove-from-project drops the ref; nothing is stopped and the project stays."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects/project-1/members", json={"ref": "terminal:terminal-1"}).status_code == 200

    with patch("imbue.system_interface.server.run_local_command_modern_version") as mock_run:
        response = client.post("/api/projects/project-1/members/remove", json={"ref": "terminal:terminal-1"})

    assert response.status_code == 200
    assert response.get_json() == {"project_id": "project-1", "members": []}
    mock_run.assert_not_called()
    # Removing a ref the project does not own is a no-op, not an error.
    assert (
        client.post("/api/projects/project-1/members/remove", json={"ref": "terminal:terminal-1"}).status_code == 200
    )
    unknown_project = client.post("/api/projects/gone/members/remove", json={"ref": "terminal:terminal-1"})
    assert unknown_project.status_code == 404


def test_share_member_adds_it_without_removing_it_anywhere(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening another project's object files it here and takes it from nowhere."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects", json={"name": "Alpha", "color": "#3B82F6", "glyph": 2}).status_code == 200
    assert client.post("/api/projects/project-1/members", json={"ref": "service:web"}).status_code == 200

    response = client.post("/api/projects/members/share", json={"ref": "service:web", "to_project_id": "alpha"})

    assert response.status_code == 200
    assert response.get_json() == {
        "ref": "service:web",
        "to_project_id": "alpha",
        "projects": ["project-1", "alpha"],
    }
    members_by_id = {
        project["project_id"]: project["members"] for project in client.get("/api/projects").get_json()["projects"]
    }
    # Still in the project that had it: a project is a view, not an owner.
    assert members_by_id == {"project-1": ["service:web"], "alpha": ["service:web"]}


def test_share_member_into_a_project_that_did_not_have_it(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat filed nowhere gets filed here, and reports the one project showing it."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    response = client.post("/api/projects/members/share", json={"ref": "chat:agent-9", "to_project_id": "project-1"})

    assert response.status_code == 200
    assert response.get_json()["projects"] == ["project-1"]
    unknown_target = client.post("/api/projects/members/share", json={"ref": "chat:agent-9", "to_project_id": "gone"})
    assert unknown_target.status_code == 404
    missing_target = client.post("/api/projects/members/share", json={"ref": "chat:agent-9"})
    assert missing_target.status_code == 400


def test_list_members_maps_every_ref_to_the_projects_showing_it(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One flat map: every filed ref plus every project showing it.

    ``/api/projects/members`` is also the routing check -- it must not resolve
    as a project whose id happens to be "members".
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects", json={"name": "Alpha", "color": "#3B82F6", "glyph": 2}).status_code == 200
    assert client.post("/api/projects/project-1/members", json={"ref": "service:web"}).status_code == 200
    assert client.post("/api/projects/alpha/members", json={"ref": "terminal:terminal-1"}).status_code == 200

    response = client.get("/api/projects/members")

    assert response.status_code == 200
    assert response.get_json() == {"members": {"service:web": ["project-1"], "terminal:terminal-1": ["alpha"]}}


def test_delete_project_panel_also_unfiles_the_member(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Destroying an object drops both its panel and its membership everywhere."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects/project-1/members", json={"ref": "terminal:terminal-1"}).status_code == 200

    response = client.post("/api/projects/panels/terminal-panel-1/delete", json={"ref": "terminal:terminal-1"})

    assert response.status_code == 200
    assert response.get_json() == {"project_ids": ["project-1"]}
    assert client.get("/api/projects").get_json()["projects"][0]["members"] == []
    # A caller that knows only the panel still works, and changes nothing here.
    panel_only = client.post("/api/projects/panels/terminal-panel-1/delete")
    assert panel_only.status_code == 200
    assert panel_only.get_json() == {"project_ids": []}


def test_set_member_title_names_the_object_machine_wide(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name is filed under the ref, so every view showing it reads the same one."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    response = client.post("/api/member-titles", json={"ref": "service:docs-viewer", "title": "  Docs  "})

    assert response.status_code == 200
    assert response.get_json() == {"ref": "service:docs-viewer", "title": "Docs"}
    assert client.get("/api/member-titles").get_json() == {"titles": {"service:docs-viewer": "Docs"}}


def test_set_member_title_overwrites_and_clears(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming again replaces the name; an empty one puts the object back to its own."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/member-titles", json={"ref": "terminal:terminal-1", "title": "Build"}).status_code == 200

    overwritten = client.post("/api/member-titles", json={"ref": "terminal:terminal-1", "title": "Deploy"})
    assert overwritten.get_json() == {"ref": "terminal:terminal-1", "title": "Deploy"}

    cleared = client.post("/api/member-titles", json={"ref": "terminal:terminal-1", "title": "   "})
    assert cleared.status_code == 200
    assert cleared.get_json() == {"ref": "terminal:terminal-1", "title": None}
    assert client.get("/api/member-titles").get_json() == {"titles": {}}


def test_member_titles_do_not_need_the_object_to_be_filed_anywhere(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown ref is named without complaint, and an unnamed one is simply absent.

    Nothing checks a ref against the machine or against a project: naming an
    object filed in no project is ordinary (Everything is where those show up),
    and a backgrounded member has no panel to hang a name on, which is exactly
    what keying by ref is for.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    response = client.post("/api/member-titles", json={"ref": "chat:agent-nowhere", "title": "Scratch"})

    assert response.status_code == 200
    assert client.get("/api/member-titles").get_json()["titles"] == {"chat:agent-nowhere": "Scratch"}
    # Clearing a name nothing ever had is a no-op rather than a 404.
    assert client.post("/api/member-titles", json={"ref": "chat:agent-elsewhere", "title": ""}).status_code == 200


def test_set_member_title_rejects_bad_bodies_and_over_long_names(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank ref, a non-string title, and a name past the cap are all 400s."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")

    assert client.post("/api/member-titles", json={"ref": " ", "title": "Docs"}).status_code == 400
    assert client.post("/api/member-titles", json={"title": "Docs"}).status_code == 400
    assert client.post("/api/member-titles", json={"ref": "service:web"}).status_code == 400
    assert client.post("/api/member-titles", json={"ref": "service:web", "title": 7}).status_code == 400

    too_long = client.post(
        "/api/member-titles",
        json={"ref": "service:web", "title": "n" * (MAX_MEMBER_TITLE_LENGTH + 1)},
    )
    assert too_long.status_code == 400
    assert str(MAX_MEMBER_TITLE_LENGTH) in too_long.get_json()["detail"]
    assert client.get("/api/member-titles").get_json() == {"titles": {}}


def test_delete_project_panel_drops_the_objects_title(
    client: FlaskClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Destroying an object drops its name, so a reused ref inherits no dead one.

    Terminal names are handed out again once a session is gone, so a name left
    behind would land on whatever answers to that ref next.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    assert client.post("/api/projects/project-1/members", json={"ref": "terminal:terminal-4"}).status_code == 200
    assert client.post("/api/member-titles", json={"ref": "terminal:terminal-4", "title": "Build"}).status_code == 200
    assert client.post("/api/member-titles", json={"ref": "chat:agent-7", "title": "Planning"}).status_code == 200

    response = client.post("/api/projects/panels/terminal-panel-4/delete", json={"ref": "terminal:terminal-4"})

    assert response.status_code == 200
    # Only the destroyed object's name goes; nothing else on the machine moves.
    assert client.get("/api/member-titles").get_json() == {"titles": {"chat:agent-7": "Planning"}}


def test_member_title_changes_broadcast_to_every_client(app: Flask) -> None:
    """Naming, renaming and destroying each announce the object's current name.

    A title belongs to the object, so a client that never opened the project
    holding it -- or that lists it backgrounded, with no panel at all -- still
    has to repaint; hence a plain broadcast rather than a layout-targeted one.
    """
    client = app.test_client()
    client_queue = _register_fake_client(app, "client-1", "desktop")

    assert client.post("/api/member-titles", json={"ref": "terminal:terminal-4", "title": "Build"}).status_code == 200
    assert _next_broadcast_message(client_queue) == {
        "type": "member_title_changed",
        "ref": "terminal:terminal-4",
        "title": "Build",
    }

    assert client.post("/api/member-titles", json={"ref": "terminal:terminal-4", "title": ""}).status_code == 200
    assert _next_broadcast_message(client_queue) == {
        "type": "member_title_changed",
        "ref": "terminal:terminal-4",
        "title": None,
    }

    assert client.post("/api/member-titles", json={"ref": "terminal:terminal-4", "title": "Build"}).status_code == 200
    assert _next_broadcast_message(client_queue)["title"] == "Build"

    assert (
        client.post("/api/projects/panels/terminal-panel-4/delete", json={"ref": "terminal:terminal-4"}).status_code
        == 200
    )
    assert _next_broadcast_message(client_queue) == {
        "type": "member_title_changed",
        "ref": "terminal:terminal-4",
        "title": None,
    }


def _app_with_registered_app(name: str) -> Flask:
    """A workspace app whose port registry holds exactly one app, under ``name``."""
    agent_manager = AgentManager.build(WebSocketBroadcaster())
    agent_manager._apps = [AppEntry(name=name, url="http://localhost:8090")]
    return create_application(build_test_state(agent_manager=agent_manager))


def _forward_port_removal_result(returncode: int, stderr: str = "") -> FinishedProcess:
    """What ``forward_port.py --remove`` looks like coming back from the runner."""
    return FinishedProcess(
        returncode=returncode,
        stdout="",
        stderr=stderr,
        command=("uv", "run", "python3", "forward_port.py", "--remove", "--name", "docs-viewer"),
        is_output_already_logged=False,
    )


def test_deregister_app_unregisters_it_and_unfiles_it_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry row goes through forward_port.py and the ref leaves every project.

    Deregistering is name-scoped rather than view-scoped: the app stops being an
    addressable service at all, so it drops out of each project showing it, not
    just the one on screen. The response says outright that nothing stopped the
    program behind the port.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    test_client = _app_with_registered_app("docs-viewer").test_client()
    assert test_client.post("/api/projects", json={"name": "Alpha", "color": "#3B82F6", "glyph": 2}).status_code == 200
    for project_id in ("project-1", "alpha"):
        assert (
            test_client.post(f"/api/projects/{project_id}/members", json={"ref": "service:docs-viewer"}).status_code
            == 200
        )

    with patch(
        "imbue.system_interface.server.run_local_command_modern_version",
        return_value=_forward_port_removal_result(0),
    ) as mock_run:
        response = test_client.post("/api/apps/docs-viewer/deregister")

    assert response.status_code == 200
    assert response.get_json() == {
        "name": "docs-viewer",
        "project_ids": ["project-1", "alpha"],
        "is_process_stopped": False,
    }
    assert mock_run.call_args.kwargs["command"] == [
        "uv",
        "run",
        "python3",
        str(_FORWARD_PORT_SCRIPT),
        "--remove",
        "--name",
        "docs-viewer",
    ]
    assert [project["members"] for project in test_client.get("/api/projects").get_json()["projects"]] == [[], []]


def test_deregister_app_reports_a_registry_removal_that_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusing forward_port.py is surfaced, and the memberships stay put."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    test_client = _app_with_registered_app("docs-viewer").test_client()
    assert (
        test_client.post("/api/projects/project-1/members", json={"ref": "service:docs-viewer"}).status_code == 200
    )

    with patch(
        "imbue.system_interface.server.run_local_command_modern_version",
        return_value=_forward_port_removal_result(2, "invalid app name"),
    ):
        response = test_client.post("/api/apps/docs-viewer/deregister")

    assert response.status_code == 500
    assert "invalid app name" in response.get_json()["detail"]
    # The app is still registered, so it must still be filed where it was filed.
    assert test_client.get("/api/projects").get_json()["projects"][0]["members"] == ["service:docs-viewer"]


def test_deregister_app_refuses_the_shell_and_unknown_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell's own row and an unregistered name are both rejected untouched."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    test_client = _app_with_registered_app("system_interface").test_client()

    with patch("imbue.system_interface.server.run_local_command_modern_version") as mock_run:
        shell = test_client.post("/api/apps/system_interface/deregister")
        unknown = test_client.post("/api/apps/docs-viewer/deregister")

    assert shell.status_code == 400
    assert unknown.status_code == 404
    mock_run.assert_not_called()


def test_deregister_app_broadcasts_the_projects_it_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clients hear the membership change, as they do for every other member mutation."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    workspace_app = _app_with_registered_app("docs-viewer")
    test_client = workspace_app.test_client()
    assert test_client.post("/api/projects/project-1/members", json={"ref": "service:docs-viewer"}).status_code == 200
    client_queue = _register_fake_client(workspace_app, "client-1", "desktop")

    with patch(
        "imbue.system_interface.server.run_local_command_modern_version",
        return_value=_forward_port_removal_result(0),
    ):
        assert test_client.post("/api/apps/docs-viewer/deregister").status_code == 200

    assert _next_broadcast_message(client_queue) == {
        "type": "project_members_changed",
        "project_ids": ["project-1"],
    }


def test_project_mutations_broadcast_to_every_client(app: Flask) -> None:
    """Create, autosave, settings, and delete each reach all connected clients."""
    client_queue = _register_fake_client(app, "client-1", "desktop")
    client = app.test_client()

    assert client.post("/api/projects", json={"name": "Alpha", "color": "#3B82F6", "glyph": 2}).status_code == 200
    assert _next_broadcast_message(client_queue) == {
        "type": "project_updated",
        "project_id": "alpha",
        "name": "Alpha",
        "color": "#3B82F6",
        "glyph": 2,
        "has_content": False,
        "members": [],
    }

    assert client.post("/api/projects/alpha", json={"layout": {}, "client_id": "client-1"}).status_code == 200
    assert _next_broadcast_message(client_queue) == {
        "type": "project_saved",
        "project_id": "alpha",
        "saved_by_client_id": "client-1",
    }

    settings = client.post("/api/projects/alpha/settings", json={"name": "Alpha", "color": "#F0603A", "glyph": 4})
    assert settings.status_code == 200
    settings_message = _next_broadcast_message(client_queue)
    assert settings_message["type"] == "project_updated"
    assert settings_message["glyph"] == 4
    assert settings_message["has_content"] is True

    assert client.post("/api/projects/alpha/delete").status_code == 200
    assert _next_broadcast_message(client_queue) == {
        "type": "project_deleted",
        "project_id": "alpha",
        "fallback_id": "project-1",
        "stopped": [],
        "failed": [],
        "left_running": [],
    }


def test_membership_changes_broadcast_the_affected_projects(app: Flask) -> None:
    """Add, remove and move each announce which projects' member lists moved.

    A move announces both ends, because the object left one project and joined
    another; a client with either one mounted has to refresh.
    """
    client = app.test_client()
    assert client.post("/api/projects", json={"name": "Alpha", "color": "#3B82F6", "glyph": 2}).status_code == 200
    client_queue = _register_fake_client(app, "client-1", "desktop")

    assert client.post("/api/projects/project-1/members", json={"ref": "service:web"}).status_code == 200
    assert _next_broadcast_message(client_queue) == {
        "type": "project_members_changed",
        "project_ids": ["project-1"],
    }

    assert (
        client.post("/api/projects/members/share", json={"ref": "service:web", "to_project_id": "alpha"}).status_code
        == 200
    )
    # Sharing only touches the destination -- nothing leaves the project it was in.
    assert _next_broadcast_message(client_queue) == {
        "type": "project_members_changed",
        "project_ids": ["alpha"],
    }

    assert client.post("/api/projects/alpha/members/remove", json={"ref": "service:web"}).status_code == 200
    assert _next_broadcast_message(client_queue) == {
        "type": "project_members_changed",
        "project_ids": ["alpha"],
    }


def test_create_chat_carries_the_project_id_beside_the_request_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``project_id`` is accepted on create-chat and is not mistaken for a chat field.

    Chat membership rides the agent's ``project`` label rather than the member
    list, so the project a chat is created in travels with the create request.
    The request model forbids unknown fields, so this guards the split.
    """
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    test_client = create_application(build_test_state()).test_client()

    response = test_client.post("/api/agents/create-chat", json={"name": "test-chat", "project_id": "alpha"})

    # Still the no-work-dir failure, i.e. the extra field reached the label path
    # rather than being rejected as an unknown request field.
    assert response.status_code == 400
    assert "project_id" not in response.get_json()["detail"]


@pytest.mark.timeout(15)
def test_websocket_snapshot_exposes_each_agent_project_label(app: Flask) -> None:
    """The agent payload the frontend already receives carries the project label.

    That label is where a chat starts out filed; an agent without one is in no
    project at all, which is ordinary -- Everything enumerates the machine, so
    it still shows up there.
    """
    agent_manager = state_of(app).agent_manager
    with agent_manager._lock:
        agent_manager._agents["chat-1"] = AgentStateItem(
            id="chat-1",
            name="filed-chat",
            state="RUNNING",
            labels={"user_created": "true", "project": "alpha"},
            work_dir=None,
        )
        agent_manager._agents["chat-2"] = AgentStateItem(
            id="chat-2",
            name="loose-chat",
            state="RUNNING",
            labels={"user_created": "true"},
            work_dir=None,
        )

    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            first = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert first["type"] == "agents_updated"
    project_by_agent_id = {agent["id"]: agent["project"] for agent in first["agents"]}
    assert project_by_agent_id == {"chat-1": "alpha", "chat-2": None}
