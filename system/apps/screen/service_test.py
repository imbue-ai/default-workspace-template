"""The routes over fakes: the state document, start on demand, sessions and identity, the lease verbs and their
projection onto neko, the display routes, and the watcher's notification."""

import json
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from flask.testing import FlaskClient
from simple_websocket import Client as WebSocketClient

from mock_neko_test import FakeNeko
from mock_supervisor_test import FakeSupervisor
from mock_window_tools_test import FakeWindowTools
from screen.data_types import Lifecycle
from screen.data_types import NekoSessionRecord
from screen.data_types import XWindow
from screen.primitives import NekoSessionId
from screen.primitives import XWindowId
from screen.service import ScreenService
from screen.service import is_session_reapable
from screen.serving import serve_in_background
from testing import start_service_stack
from testing import wait_until

_IDENTITY = {"X-Imbue-Identity": json.dumps({"owner": True, "user_id": "u1", "email": "josh@imbue.com"})}
_VISITOR = {"X-Imbue-Identity": json.dumps({"owner": False, "user_id": "u2", "email": "mia@example.com"})}
_AGENT_A = {"X-Mngr-Agent-Id": "chat-a", "X-Mngr-Agent-Name": "Alice"}
_AGENT_B = {"X-Mngr-Agent-Id": "chat-b", "X-Mngr-Agent-Name": "Bob"}


def _session(client: FlaskClient, headers: dict[str, str]) -> dict:
    answer = client.post("/api/session", headers=headers)
    assert answer.status_code == 200, answer.get_json()
    return answer.get_json()


def test_state_document_before_any_start_is_stopped_and_free(client: FlaskClient) -> None:
    document = client.get("/api/state").get_json()
    assert document["lifecycle"] == "stopped"
    assert document["title"] == "Screen"
    assert document["video_id"] == "watch"
    assert document["control"]["controller"] == "nobody"
    assert document["screen_size"] is None
    assert document["chromium"] == {"state": "absent"}


def test_a_page_load_starts_the_stack(
    client: FlaskClient, service: ScreenService, fake_supervisor: FakeSupervisor
) -> None:
    answer = client.get("/")
    assert answer.status_code == 200
    assert b"has not been built" in answer.data
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING)
    assert ("start", "screen-neko") in fake_supervisor.calls


def test_the_browser_launch_path_starts_the_stack_and_answers_the_root(
    client: FlaskClient, service: ScreenService
) -> None:
    answer = client.post("/launch/browser", json={"url": "https://example.com", "client_id": "c1"})
    assert answer.status_code == 200
    assert answer.get_json() == {"path": "/"}
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING)


def test_connect_info_is_empty_in_this_wave(client: FlaskClient) -> None:
    assert client.get("/api/connect-info").get_json() == {"ice_servers": [], "candidates": [], "expires_at": None}


def test_session_is_logged_in_with_the_identitys_name(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    start_service_stack(service)
    owner = _session(client, _IDENTITY)
    assert owner["name"] == "josh"
    assert owner["is_owner"] is True
    assert owner["token"].startswith("tok-")
    visitor = _session(client, _VISITOR)
    assert visitor["name"] == "mia"
    assert visitor["is_owner"] is False
    anonymous = _session(client, {})
    assert anonymous["name"] == "Owner"
    assert fake_neko.calls.count("login:josh") == 1
    assert fake_neko.sessions[visitor["session_id"]].is_admin is False


def test_session_before_the_stack_runs_is_refused_and_starts_it(client: FlaskClient, service: ScreenService) -> None:
    answer = client.post("/api/session", headers=_IDENTITY)
    assert answer.status_code == 503
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING)


def test_human_take_and_return_project_onto_neko(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    start_service_stack(service)
    session = _session(client, _IDENTITY)
    taken = client.post("/api/control/take", json={"session_id": session["session_id"]})
    assert taken.status_code == 200
    assert taken.get_json()["controller"] == "human"
    assert taken.get_json()["human_name"] == "josh"
    assert wait_until(lambda: fake_neko.host_id == session["session_id"])
    assert fake_neko.is_locked is False
    returned = client.post("/api/control/return", json={"session_id": session["session_id"]})
    assert returned.get_json()["ok"] is True
    assert returned.get_json()["controller"] == "nobody"
    assert wait_until(lambda: "reset" in fake_neko.calls)
    assert fake_neko.host_id is None


def test_take_control_needs_a_known_session(client: FlaskClient, service: ScreenService) -> None:
    start_service_stack(service)
    assert client.post("/api/control/take", json={"session_id": "nope"}).status_code == 404
    assert client.post("/api/control/take", json={}).status_code == 400


def test_take_control_while_stopped_is_refused(client: FlaskClient) -> None:
    assert client.post("/api/control/take", json={"session_id": "s1"}).status_code == 409


def test_agent_acquire_locks_neko_and_a_human_take_queues_the_agent(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    start_service_stack(service)
    acquired = client.post("/api/control/acquire", json={}, headers=_AGENT_A).get_json()
    assert acquired["ok"] is True
    assert acquired["status"] == "acquired"
    assert wait_until(lambda: fake_neko.is_locked is True)
    busy = client.post("/api/control/acquire", json={}, headers=_AGENT_B).get_json()
    assert busy == {**busy, "ok": False, "status": "busy_agent", "enqueued": True}
    assert busy["waiting"] == ["Bob"]
    session = _session(client, _IDENTITY)
    client.post("/api/control/take", json={"session_id": session["session_id"]})
    state = client.get("/api/state").get_json()["control"]
    assert state["controller"] == "human"
    assert state["waiting"] == ["Alice", "Bob"]
    assert wait_until(lambda: fake_neko.is_locked is False and fake_neko.host_id == session["session_id"])
    refused = client.post("/api/control/acquire", json={}, headers=_AGENT_A).get_json()
    assert refused["status"] == "busy_human"
    reclaimed = client.post("/api/control/acquire", json={"reclaim": True}, headers=_AGENT_A).get_json()
    assert reclaimed["ok"] is True


def test_agent_release_and_handoff(client: FlaskClient, service: ScreenService) -> None:
    start_service_stack(service)
    assert client.post("/api/control/release", json={}, headers=_AGENT_A).get_json()["released"] is False
    client.post("/api/control/acquire", json={}, headers=_AGENT_A)
    handed = client.post("/api/control/handoff", json={"reason": "solve the CAPTCHA"}, headers=_AGENT_A).get_json()
    assert handed["ok"] is True
    assert handed["status"] == "handed_off"
    assert handed["controller"] == "nobody"
    assert handed["waiting"] == ["Alice"]
    assert client.post("/api/control/handoff", json={}, headers=_AGENT_B).get_json()["status"] == "not_owner"


def test_agent_verbs_need_the_agent_header(client: FlaskClient, service: ScreenService) -> None:
    start_service_stack(service)
    for path in ("/api/control/acquire", "/api/control/release", "/api/control/handoff"):
        assert client.post(path, json={}).status_code == 400, path


def test_agent_acquire_while_stopped_starts_the_stack_and_says_starting(
    client: FlaskClient, service: ScreenService
) -> None:
    answer = client.post("/api/control/acquire", json={}, headers=_AGENT_A).get_json()
    assert answer["ok"] is False
    assert answer["status"] == "starting"
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING)


def test_screenshot_is_png_by_default_and_jpeg_on_request(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    assert client.get("/api/screenshot").status_code == 409
    start_service_stack(service)
    png = client.get("/api/screenshot?quality=70")
    assert png.status_code == 200
    assert png.mimetype == "image/png"
    assert png.data.startswith(b"\x89PNG")
    assert "shot:70" in fake_neko.calls
    jpeg = client.get("/api/screenshot?format=jpeg")
    assert jpeg.mimetype == "image/jpeg"
    assert jpeg.data == fake_neko.screenshot_bytes
    assert client.get("/api/screenshot?quality=0").status_code == 400
    assert client.get("/api/screenshot?format=gif").status_code == 400


def test_windows_are_listed_and_activated(client: FlaskClient, service: ScreenService) -> None:
    tools = service.window_tools
    assert isinstance(tools, FakeWindowTools)
    tools.windows.append(
        XWindow(
            id=XWindowId("0x03600001"),
            desktop=0,
            pid=1,
            x=1,
            y=2,
            width=3,
            height=4,
            window_class="xeyes.XEyes",
            title="xeyes",
        )
    )
    assert client.get("/api/windows").status_code == 409
    start_service_stack(service)
    listed = client.get("/api/windows").get_json()["windows"]
    assert [window["title"] for window in listed] == ["xeyes"]
    assert client.post("/api/windows/0x03600001/activate").get_json() == {"activated": True}
    assert tools.activated == ["0x03600001"]
    assert client.post("/api/windows/nonsense/activate").status_code == 400


def test_run_starts_the_stack_and_launches_the_command(client: FlaskClient, service: ScreenService) -> None:
    answer = client.post("/api/run", json={"command": ["xeyes", "-geometry", "200x100"]})
    assert answer.status_code == 200
    assert answer.get_json()["pid"] == 4243
    assert client.post("/api/run", json={"command": []}).status_code == 400
    assert client.post("/api/run", json={"command": "xeyes"}).status_code == 400


def test_title_follows_the_active_window(client: FlaskClient, service: ScreenService) -> None:
    start_service_stack(service)
    tools = service.window_tools
    assert isinstance(tools, FakeWindowTools)
    tools.active_title = "Firefox"
    service.poll_title()
    assert client.get("/api/state").get_json()["title"] == "Firefox"
    tools.active_title = None
    service.poll_title()
    assert client.get("/api/state").get_json()["title"] == "Screen"


def test_watcher_notification_stops_the_lifecycle_and_frees_the_lease(
    client: FlaskClient, service: ScreenService
) -> None:
    start_service_stack(service)
    client.post("/api/control/acquire", json={}, headers=_AGENT_A)
    answer = client.post(
        "/api/internal/stopped", json={"member": "screen-xorg", "reason": "screen-xorg exited unexpectedly"}
    )
    assert answer.status_code == 204
    document = client.get("/api/state").get_json()
    assert document["lifecycle"] == "stopped"
    assert document["stop_reason"] == "screen-xorg exited unexpectedly"
    assert document["control"]["controller"] == "nobody"
    assert client.post("/api/session", headers=_IDENTITY).status_code == 503


def test_watcher_notification_is_loopback_only(client: FlaskClient) -> None:
    answer = client.post("/api/internal/stopped", json={"member": "x"}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert answer.status_code == 403


def test_neko_host_events_record_an_implicit_human_and_its_leaving(
    client: FlaskClient, service: ScreenService
) -> None:
    start_service_stack(service)
    session = _session(client, _IDENTITY)
    service.handle_neko_event(
        "control/host", {"id": session["session_id"], "has_host": True, "host_id": session["session_id"]}
    )
    control = client.get("/api/state").get_json()["control"]
    assert control["controller"] == "human"
    assert control["human_pinned"] is False
    assert control["human_name"] == "josh"
    service.handle_neko_event("control/host", {"id": session["session_id"], "has_host": False})
    assert client.get("/api/state").get_json()["control"]["controller"] == "nobody"


def test_the_services_own_session_becoming_host_is_ignored(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    start_service_stack(service)
    own = next(record for record in fake_neko.sessions.values() if record.is_admin)
    service.handle_neko_event("control/host", {"id": own.session_id, "has_host": True, "host_id": own.session_id})
    assert client.get("/api/state").get_json()["control"]["controller"] == "nobody"


def test_screen_size_follows_nekos_events(client: FlaskClient, service: ScreenService) -> None:
    start_service_stack(service)
    service.handle_neko_event("system/init", {"screen_size": {"width": 1280, "height": 720, "rate": 60}})
    assert client.get("/api/state").get_json()["screen_size"] == {"width": 1280, "height": 720, "rate": 60}
    service.handle_neko_event("screen/updated", {"id": "x", "width": 1600, "height": 900, "rate": 60})
    assert client.get("/api/state").get_json()["screen_size"]["width"] == 1600


def test_disconnected_sessions_are_reaped_after_ten_minutes(
    client: FlaskClient, service: ScreenService, fake_neko: FakeNeko
) -> None:
    start_service_stack(service)
    session = _session(client, _IDENTITY)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=11)).isoformat()
    recent = (now - timedelta(minutes=2)).isoformat()
    fake_neko.sessions[session["session_id"]] = fake_neko.sessions[session["session_id"]].model_copy(
        update={"is_connected": False, "not_connected_since": old}
    )
    fake_neko.sessions["fresh"] = NekoSessionRecord(
        session_id=NekoSessionId("fresh"), name="x", is_admin=False, is_connected=False, not_connected_since=recent
    )
    assert service.reap_disconnected_sessions(now) == 1
    assert session["session_id"] not in fake_neko.sessions
    assert "fresh" in fake_neko.sessions
    assert is_session_reapable(None, False, now) is False
    assert is_session_reapable(old, True, now) is False


def test_the_events_socket_sends_the_state_first_and_then_changes(app, service: ScreenService) -> None:
    with serve_in_background("127.0.0.1", 0, app) as server:
        port = server.socket.getsockname()[1]
        socket_client = WebSocketClient.connect(f"ws://127.0.0.1:{port}/api/ws")
        try:
            first = json.loads(socket_client.receive(timeout=5))
            assert first["type"] == "state"
            assert first["lifecycle"] == "stopped"
            start_service_stack(service)
            seen: list[str] = []
            deadline_reached = False
            while not deadline_reached:
                message = socket_client.receive(timeout=5)
                assert message is not None
                document = json.loads(message)
                seen.append(document["lifecycle"])
                if document["lifecycle"] == "running":
                    deadline_reached = True
            assert "starting" in seen
        finally:
            socket_client.close()


def test_state_broadcasts_do_not_block_when_no_viewer_listens(service: ScreenService) -> None:
    start_service_stack(service)
    done = threading.Event()

    def poll_many() -> None:
        for _ in range(50):
            service.poll_title()
        done.set()

    threading.Thread(target=poll_many).start()
    assert done.wait(timeout=5.0)


def test_lease_checkpoint_is_written_under_the_state_dir(
    client: FlaskClient, service: ScreenService, tmp_path: Path
) -> None:
    start_service_stack(service)
    client.post("/api/control/acquire", json={}, headers=_AGENT_A)
    record = json.loads((tmp_path / "state" / "control.json").read_text())
    assert record["controller"] == "AGENT"
    assert record["owner_agent_id"] == "chat-a"
