"""Tests for the routes over fake sources: the page, the summary, the write guard, and chat actions."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from activity.memory_reading import MemorySources
from activity.pages import build_pages_blueprint
from activity.readings import ReadingSources
from activity.testing import FakeChatApp
from activity.testing import chat_snapshot
from activity.testing import process_info
from activity.testing import write_fake_process
from activity.testing import write_registry

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_JSON = "application/json"


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_app: FakeChatApp) -> FlaskClient:
    proc_dir = tmp_path / "proc"
    write_fake_process(proc_dir, 415, "supervisord", 1, 31 * 1024, -1000, ["supervisord"])
    write_fake_process(proc_dir, 544, "chat-app", 415, 160 * 1024, 25, ["chat-app"])
    write_fake_process(proc_dir, 13106, "claude", 13065, 330 * 1024, 480, ["claude", "--resume"])
    cgroup_dir = tmp_path / "cgroup"
    cgroup_dir.mkdir()
    (cgroup_dir / "memory.max").write_text(str(8 * 1024**3))
    (cgroup_dir / "memory.current").write_text(str(2 * 1024**3))
    runtime_dir = tmp_path / "oom_priority"
    (runtime_dir / "agent_pids").mkdir(parents=True)
    (runtime_dir / "agent_pids" / "13106.json").write_text(
        json.dumps({"agent_name": "wallpaper", "is_worker": False, "agent_id": "agent-a"})
    )
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(runtime_dir))
    registry_path = tmp_path / "apps.toml"
    write_registry(
        registry_path,
        [
            {"name": "chat", "url": "http://chat.test", "display_name": "Chat", "program": "chat", "critical": True},
            {
                "name": "activity",
                "url": "http://localhost:8040",
                "label": "activity-as27k3mv",
                "display_name": "Activity",
            },
            {
                "name": "files",
                "url": "http://localhost:8300",
                "display_name": "File Viewer",
                "program": "files",
                "stop_when_no_windows": True,
            },
        ],
    )
    app = Flask("activity-under-test", static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            static_directory=tmp_path / "static",
            contract_path=tmp_path / "app_contract.js",
            sources=ReadingSources(
                memory=MemorySources(
                    host_meminfo_path=tmp_path / "no-host-meminfo",
                    cgroup_dir=cgroup_dir,
                    proc_meminfo_path=tmp_path / "no-meminfo",
                ),
                proc_dir=proc_dir,
                registry_path=registry_path,
            ),
            client=chat_app.client(),
            read_process_info=lambda: [process_info("chat", "RUNNING", 544), process_info("files", "STOPPED", 0)],
            now=lambda: _NOW,
        )
    )
    return app.test_client()


def _chat_app(status: str) -> FakeChatApp:
    return FakeChatApp([chat_snapshot("c1", "Wallpaper", status, "agent-a", "claude")])


def test_the_page_says_it_is_not_built_until_the_bundle_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch, _chat_app("idle"))
    assert b"not been built" in client.get("/").data
    assert client.get("/api/health").get_json() == {"status": "ok", "is_frontend_built": False}
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "index.html").write_text("<title>Activity</title>")
    assert client.get("/").data == b"<title>Activity</title>"
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/_static/app_contract.js").status_code == 404


def test_the_summary_credits_the_chat_and_reads_the_cgroup_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = _client(tmp_path, monkeypatch, _chat_app("idle")).get("/api/summary").get_json()
    assert summary["memory"]["limit_bytes"] == 8 * 1024**3
    assert summary["memory"]["source"] == "CGROUP"
    assert [(chat["name"], chat["rss_kib"] // 1024, chat["harness"]) for chat in summary["chats"]] == [
        ("Wallpaper", 330, "claude")
    ]
    assert {app["name"]: app["state"] for app in summary["apps"]} == {"Chat": "RUNNING", "File Viewer": "STOPPED"}
    assert summary["notes"] == []


def test_a_write_that_is_not_json_or_comes_from_another_origin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_app = _chat_app("idle")
    client = _client(tmp_path, monkeypatch, chat_app)
    assert client.post("/api/chats/c1/stop").status_code == 403
    assert (
        client.post("/api/chats/c1/stop", json={}, headers={"Origin": "https://elsewhere.example"}).status_code == 403
    )
    assert chat_app.actions == []


def test_stopping_an_idle_chat_goes_through_the_chat_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chat_app = _chat_app("idle")
    response = _client(tmp_path, monkeypatch, chat_app).post(
        "/api/chats/c1/stop", json={"is_interrupt_confirmed": False}
    )
    assert response.status_code == 200
    assert chat_app.actions == [("c1", "stop")]


def test_a_stop_with_the_registry_unreadable_goes_to_the_chat_apps_default_port_as_the_summary_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_app = _chat_app("idle")
    client = _client(tmp_path, monkeypatch, chat_app)
    (tmp_path / "apps.toml").unlink()
    response = client.post("/api/chats/c1/stop", json={"is_interrupt_confirmed": False})
    assert response.status_code == 200
    assert chat_app.actions == [("c1", "stop")]
    assert set(chat_app.hosts) == {"127.0.0.1:8010"}


def test_a_chat_that_started_working_is_not_interrupted_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_app = _chat_app("working")
    client = _client(tmp_path, monkeypatch, chat_app)
    refused = client.post("/api/chats/c1/stop", json={"is_interrupt_confirmed": False})
    assert refused.status_code == 409
    assert refused.get_json()["chat_status"] == "working"
    assert chat_app.actions == []
    confirmed = client.post("/api/chats/c1/stop", json={"is_interrupt_confirmed": True})
    assert confirmed.status_code == 200
    assert chat_app.actions == [("c1", "stop")]


def test_an_unknown_chat_or_action_is_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch, _chat_app("idle"))
    assert client.post("/api/chats/nope/stop", json={}).status_code == 404
    assert client.post("/api/chats/c1/explode", content_type=_JSON, data="{}").status_code == 404


def test_starting_a_chat_needs_no_recheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chat_app = FakeChatApp([chat_snapshot("c1", "Wallpaper", "stopped", "agent-a", "claude")])
    assert _client(tmp_path, monkeypatch, chat_app).post("/api/chats/c1/start", json={}).status_code == 200
    assert chat_app.actions == [("c1", "start")]


def test_starting_requires_a_chat_the_chat_app_lists_and_odd_ids_never_reach_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat_app = _chat_app("stopped")
    client = _client(tmp_path, monkeypatch, chat_app)
    for odd_id in ("..", "create%23", "create%3F", "nope"):
        assert client.post(f"/api/chats/{odd_id}/start", json={}).status_code == 404
    assert chat_app.actions == []


def test_only_the_workspaces_owner_reaches_the_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chat_app = _chat_app("idle")
    client = _client(tmp_path, monkeypatch, chat_app)
    visitor = {"X-Imbue-Identity": json.dumps({"owner": False, "user_id": "u2", "email": "guest@example.com"})}
    owner = {"X-Imbue-Identity": json.dumps({"owner": True, "user_id": "u1", "email": "me@example.com"})}
    assert client.get("/api/summary", headers=visitor).status_code == 403
    assert client.post("/api/chats/c1/stop", json={}, headers=visitor).status_code == 403
    assert client.get("/api/health", headers=visitor).status_code == 200
    assert client.get("/api/summary", headers=owner).status_code == 200
    assert client.get("/api/summary", headers={"X-Imbue-Identity": "not json"}).status_code == 200
    assert chat_app.actions == []


def test_a_stop_from_the_desktop_page_goes_through_although_the_forwarder_dropped_its_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The desktop's forwarder drops the browser's Host, so the app sees its loopback address while the page's Origin
    is the app's own desktop origin; the browser's Sec-Fetch-Site says the request is the page's own."""
    chat_app = _chat_app("idle")
    client = _client(tmp_path, monkeypatch, chat_app)
    desktop_origin = "https://activity-as27k3mv.agent-1f2e3d.localhost:8421"
    response = client.post(
        "/api/chats/c1/stop",
        json={"is_interrupt_confirmed": False},
        headers={"Origin": desktop_origin, "Sec-Fetch-Site": "same-origin", "Host": "localhost:8040"},
    )
    assert response.status_code == 200
    assert chat_app.actions == [("c1", "stop")]


def test_another_apps_page_cannot_stop_a_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chat_app = _chat_app("idle")
    client = _client(tmp_path, monkeypatch, chat_app)
    other_app = "https://files-ab12cd34.agent-1f2e3d.localhost:8421"
    assert (
        client.post(
            "/api/chats/c1/stop", json={}, headers={"Origin": other_app, "Sec-Fetch-Site": "same-site"}
        ).status_code
        == 403
    )
    assert client.post("/api/chats/c1/stop", json={}, headers={"Origin": other_app}).status_code == 403
    own_label_without_fetch_site = "https://activity-as27k3mv.agent-1f2e3d.localhost:8421"
    assert (
        client.post("/api/chats/c1/stop", json={}, headers={"Origin": own_label_without_fetch_site}).status_code == 200
    )
    assert chat_app.actions == [("c1", "stop")]
