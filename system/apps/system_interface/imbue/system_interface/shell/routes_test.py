"""Tests for the shell's HTTP routes (contracts.md sections 5 and 6) over a test state with a fake-fed inventory."""

import queue
from pathlib import Path
from typing import Any

import pytest
from app_instances.testing import StubInstanceSource
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import state_of
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.inventory import HttpInstanceFetcher
from imbue.system_interface.shell.liveness import probe_all_app_liveness
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.testing import FakeInstanceFetcher
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import layout_showing
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import FakeSupervisorServer
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_TERMINAL_URL = "http://localhost:7681"
_TERMINAL_1 = Address("app:terminal?instance=terminal-1")
_TERMINAL_2 = Address("app:terminal?instance=terminal-2")
_FILES = Address("app:files")
_TAB = TabId("tab-000000000000000a")
_NOT_LOOPBACK = {"REMOTE_ADDR": "10.0.0.7"}


def _registry(tmp_path: Path, *extra_rows: str) -> Path:
    return write_registry(
        tmp_path / "apps.toml",
        registry_row_toml(
            "terminal",
            _TERMINAL_URL,
            True,
            program="terminal",
            actions=[("new", "New terminal")],
            default_shortcut=("new", "new"),
        ),
        registry_row_toml("files", "http://localhost:7000", program="files", default_shortcut=("open", "focus")),
        *extra_rows,
    )


def _app(tmp_path: Path, inventory: AppInventory, broadcaster: WebSocketBroadcaster) -> Flask:
    """The shell app over ``inventory``; the agent manager shares the inventory's broadcaster, as in production."""
    state = build_test_state(
        agent_manager=AgentManager.build(broadcaster), shell_state_directory=tmp_path / "state", inventory=inventory
    )
    return create_application(state)


def _shell(app: Flask) -> ShellState:
    return state_of(app).shell


def _register_client(app: Flask, client_id: str, view_id: str) -> "queue.Queue[str | None]":
    client_queue = _shell(app).broadcaster.register()
    _shell(app).broadcaster.set_client_info(client_queue, client_id, view_id, "desktop")
    return client_queue


@pytest.fixture
def fetcher() -> FakeInstanceFetcher:
    fetcher = FakeInstanceFetcher()
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1", "Terminal 1"))
    return fetcher


@pytest.fixture
def app(tmp_path: Path, broadcaster: WebSocketBroadcaster, fetcher: FakeInstanceFetcher) -> Flask:
    inventory = build_inventory(_registry(tmp_path), broadcaster, fetcher=fetcher)
    inventory.refetch_now("terminal")
    return _app(tmp_path, inventory, broadcaster)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


# ---------- section 5 ----------


def test_an_app_nudge_is_accepted_from_loopback_only(client: FlaskClient, app: Flask) -> None:
    try:
        assert client.post("/api/apps/terminal/changed").status_code == 204
        assert client.post("/api/apps/unknown/changed").status_code == 404
        assert client.post("/api/apps/terminal/changed", environ_base=_NOT_LOOPBACK).status_code == 403
    finally:
        _shell(app).inventory.stop()


def test_a_tab_report_rebinds_the_tab_everywhere_and_files_it_in_the_project(
    client: FlaskClient, app: Flask, fetcher: FakeInstanceFetcher
) -> None:
    shell = _shell(app)
    shell.projects.create_project("Alpha", "#111111", 0, ())
    shell.layouts.save_layout("alpha", "c1", layout_showing(_TERMINAL_1), TEST_NOW)
    shell.layouts.save_layout("everything", "c2", layout_showing(_TERMINAL_1), TEST_NOW)
    fetcher.list(_TERMINAL_URL, instance_record("terminal-1"), instance_record("terminal-2"))
    client_queue = _register_client(app, "c1", "alpha")

    response = client.post("/api/tabs/tab-0000000000000000/instance", json={"app": "terminal", "key": "terminal-2"})

    assert response.status_code == 204
    assert shell.layouts.read_layout("alpha", "c1", DeviceKind.DESKTOP).tabs["p0"].address == _TERMINAL_2
    assert shell.layouts.read_layout("everything", "c2", DeviceKind.DESKTOP).tabs["p0"].address == _TERMINAL_2
    assert shell.projects.get_project("alpha").tabs == (_TERMINAL_2,)
    messages = drain_messages(client_queue)
    rebound = [message for message in messages if message["type"] == "tab_rebound"]
    assert {(message["client_id"], message["view_id"]) for message in rebound} == {
        ("c1", "alpha"),
        ("c2", "everything"),
    }
    assert rebound[0]["address"] == str(_TERMINAL_2) and rebound[0]["tab_id"] == "tab-0000000000000000"
    assert [message["type"] for message in messages][-1] == "apps_updated"
    assert shell.inventory.find_instance(_TERMINAL_2) is not None


def test_a_tab_report_is_refused_when_it_names_no_tab_or_the_wrong_app(client: FlaskClient, app: Flask) -> None:
    _shell(app).layouts.save_layout("everything", "c1", layout_showing(_FILES), TEST_NOW)
    assert (
        client.post("/api/tabs/tab-00000000000000ff/instance", json={"app": "terminal", "key": "k"}).status_code == 404
    )
    assert (
        client.post("/api/tabs/tab-0000000000000000/instance", json={"app": "terminal", "key": "k"}).status_code == 400
    )
    assert client.post("/api/tabs/nope/instance", json={"app": "terminal", "key": "k"}).status_code == 400
    assert client.post("/api/tabs/tab-0000000000000000/instance", json={"app": "terminal"}).status_code == 400
    assert (
        client.post(
            "/api/tabs/tab-0000000000000000/instance", json={"app": "files", "key": ""}, environ_base=_NOT_LOOPBACK
        ).status_code
        == 403
    )


def test_client_activity_is_appended_by_kind(client: FlaskClient, app: Flask) -> None:
    base = {"client_id": "c1", "device_kind": "desktop", "view_id": "everything"}
    assert (
        client.post(
            "/api/client-activity", json={**base, "kind": "message", "app": "chat", "key": "agent-1", "text": "hi"}
        ).status_code
        == 204
    )
    assert (
        client.post("/api/client-activity", json={**base, "kind": "view_switch", "from_view_id": "alpha"}).status_code
        == 204
    )
    assert client.post("/api/client-activity", json={**base, "kind": "nope"}).status_code == 400
    assert (
        client.post("/api/client-activity", json={**base, "kind": "message"}, environ_base=_NOT_LOOPBACK).status_code
        == 403
    )
    events = _shell(app).activity.read_events()
    assert [(event["type"], event["client_id"]) for event in events] == [("message", "c1"), ("view_switch", "c1")]
    assert events[0]["key"] == "agent-1" and events[1]["from_view_id"] == "alpha"


# ---------- section 6: the relay ----------


def test_instance_verbs_are_relayed_and_the_list_refetched(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, stub_source: StubInstanceSource, stub_app_url: str
) -> None:
    stub_source.records.append(instance_record("stub-1"))
    inventory = build_inventory(
        write_registry(
            tmp_path / "apps.toml", registry_row_toml("stub", stub_app_url, True, actions=[("new", "New")])
        ),
        broadcaster,
        fetcher=HttpInstanceFetcher(),
    )
    inventory.refetch_now("stub")
    client = _app(tmp_path, inventory, broadcaster).test_client()

    created = client.post("/api/apps/stub/instances", json={"action": "new", "params": {}})
    assert created.status_code == 201 and created.get_json()["instance"]["key"] == "stub-2"
    assert inventory.find_instance(Address("app:stub?instance=stub-2")) is not None
    renamed = client.post("/api/apps/stub/instances/stub-2/rename", json={"title": "Renamed"})
    assert renamed.status_code == 200
    found = inventory.find_instance(Address("app:stub?instance=stub-2"))
    assert found is not None and found[1].title == "Renamed"
    assert client.post("/api/apps/stub/instances/stub-2/location", json={"path": "/deeper"}).status_code == 200
    assert client.post("/api/apps/stub/instances/stub-2/delete").status_code == 204
    assert inventory.find_instance(Address("app:stub?instance=stub-2")) is None
    assert client.post("/api/apps/stub/instances/stub-9/rename", json={"title": "x"}).status_code == 404
    assert client.post("/api/apps/unknown/instances", json={"action": "new", "params": {}}).status_code == 404


# ---------- section 6: stop and start ----------


def test_stop_and_start_drive_the_supervised_program(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, fake_supervisor: FakeSupervisorServer
) -> None:
    fake_supervisor.statename_by_program["files"] = "RUNNING"
    fake_supervisor.statename_by_program["system_interface"] = "RUNNING"
    registry_path = _registry(
        tmp_path,
        registry_row_toml("system_interface", "http://localhost:8000", program="system_interface", is_critical=True),
        registry_row_toml("chat", "http://localhost:8000", True, program="system_interface"),
        registry_row_toml(
            "plain",
            "http://localhost:1",
        ),
    )
    inventory = build_inventory(registry_path, broadcaster, prober=probe_all_app_liveness)
    client = _app(tmp_path, inventory, broadcaster).test_client()

    stopped = client.post("/api/apps/files/stop")
    assert stopped.status_code == 200 and stopped.get_json() == {"name": "files", "is_running": False}
    assert fake_supervisor.statename_by_program["files"] == "STOPPED"
    started = client.post("/api/apps/files/start")
    assert started.status_code == 200 and started.get_json() == {"name": "files", "is_running": True}

    assert client.post("/api/apps/system_interface/stop").status_code == 400
    assert client.post("/api/apps/chat/stop").status_code == 400
    assert client.post("/api/apps/plain/stop").status_code == 400
    assert client.post("/api/apps/unknown/stop").status_code == 404


def test_an_unreachable_supervisord_is_a_502(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_SUPERVISOR_SOCKET", str(tmp_path / "missing.sock"))
    client = _app(tmp_path, build_inventory(_registry(tmp_path), broadcaster), broadcaster).test_client()
    assert client.post("/api/apps/files/stop").status_code == 502


# ---------- section 6: projects ----------


def test_projects_are_created_seeded_and_listed(client: FlaskClient, app: Flask) -> None:
    client_queue = _register_client(app, "c1", "everything")
    created = client.post("/api/projects", json={"name": "Research", "color": "#12B5A5", "glyph": 4})
    assert created.status_code == 201
    assert created.get_json() == {
        "id": "research",
        "name": "Research",
        "color": "#12B5A5",
        "glyph": 4,
        "tabs": [],
        "shortcuts": [
            {"app": "terminal", "action": "new", "mode": "new"},
            {"app": "files", "action": "open", "mode": "focus"},
        ],
    }
    assert client.get("/api/projects").get_json()["projects"][0]["id"] == "research"
    assert client.post("/api/projects", json={"name": "research!", "color": "#12B5A5", "glyph": 4}).status_code == 409
    assert client.post("/api/projects", json={"name": "Bad", "color": "red", "glyph": 4}).status_code == 400
    assert client.post("/api/projects", json={"name": "Bad"}).status_code == 400
    assert [message["type"] for message in drain_messages(client_queue)] == ["projects_updated"]


def test_project_settings_tabs_shortcuts_and_deletion(client: FlaskClient, app: Flask) -> None:
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    client.post("/api/projects", json={"name": "Beta", "color": "#111111", "glyph": 1})
    _shell(app).layouts.save_layout("alpha", "c1", layout_showing(_TERMINAL_1), TEST_NOW)

    settings = client.post("/api/projects/alpha/settings", json={"name": "Alpha 2", "color": "#222222", "glyph": 2})
    assert settings.status_code == 200 and settings.get_json()["name"] == "Alpha 2"
    assert (
        client.post(
            "/api/projects/everything/settings", json={"name": "x", "color": "#222222", "glyph": 2}
        ).status_code
        == 404
    )
    assert (
        client.post("/api/projects/missing/settings", json={"name": "x", "color": "#222222", "glyph": 2}).status_code
        == 404
    )

    added = client.post("/api/projects/alpha/tabs", json={"address": str(_TERMINAL_1)})
    assert added.status_code == 200 and added.get_json()["tabs"] == [str(_TERMINAL_1)]
    assert client.post("/api/projects/alpha/tabs", json={"address": "terminal:terminal-1"}).status_code == 400
    removed = client.post("/api/projects/alpha/tabs/remove", json={"address": str(_TERMINAL_1)})
    assert removed.status_code == 200 and removed.get_json()["tabs"] == []

    assert (
        client.post(
            "/api/projects/alpha/shortcuts", json={"app": "terminal", "action": "open", "mode": "new"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/projects/alpha/shortcuts", json={"app": "nope", "action": "open", "mode": "new"}).status_code
        == 400
    )
    flipped = client.post("/api/projects/alpha/shortcuts", json={"app": "terminal", "action": "new", "mode": "focus"})
    assert flipped.status_code == 200
    assert flipped.get_json()["shortcuts"][0] == {"app": "terminal", "action": "new", "mode": "focus"}
    pruned = client.post("/api/projects/alpha/shortcuts/remove", json={"app": "files", "action": "open"})
    assert [shortcut["app"] for shortcut in pruned.get_json()["shortcuts"]] == ["terminal"]

    deleted = client.post("/api/projects/alpha/delete")
    assert deleted.status_code == 200 and deleted.get_json() == {"fallback_view_id": "beta"}
    assert not (_shell(app).state_directory / "layouts" / "alpha").exists()
    assert client.post("/api/projects/alpha/delete").status_code == 404


# ---------- section 6: layouts ----------


def test_layouts_are_read_per_client_with_the_seed_as_fallback(client: FlaskClient, app: Flask) -> None:
    assert client.get("/api/layouts/missing?client=c1").status_code == 404
    assert client.get("/api/layouts/everything").status_code == 400
    empty = client.get("/api/layouts/everything?client=c1&device=mobile").get_json()
    assert empty == {"dockview": None, "tabs": {}, "device_kind": "mobile", "updated_at": None}

    body = {
        "client_id": "c1",
        "device_kind": "desktop",
        "dockview": {"panels": {"p0": {}}},
        "tabs": {"p0": {"address": str(_TERMINAL_1), "tab_id": str(_TAB), "last_focused_ms": 5}},
    }
    assert client.post("/api/layouts/everything", json=body).status_code == 204
    assert client.post("/api/layouts/missing", json=body).status_code == 404
    assert client.post("/api/layouts/everything", json={"client_id": "c1"}).status_code == 400

    own = client.get("/api/layouts/everything?client=c1").get_json()
    assert own["tabs"]["p0"]["address"] == str(_TERMINAL_1) and own["updated_at"] is not None
    seeded = client.get("/api/layouts/everything?client=c2&device=desktop").get_json()
    assert seeded["tabs"] == own["tabs"]
    assert client.get("/api/layouts/everything?client=c2&device=mobile").get_json()["tabs"] == {}


# ---------- the broadcast endpoint ----------


def _broadcast(client: FlaskClient, op: str, args: dict[str, Any] | None = None, agent_id: str = "agent-1") -> Any:
    return client.post("/api/layout/broadcast", json={"op": op, "args": args or {}, "agent_id": agent_id})


def test_the_broadcast_endpoint_validates_its_input(client: FlaskClient) -> None:
    assert _broadcast(client, "views").status_code == 200
    assert client.post("/api/layout/broadcast", json={"op": "views"}, environ_base=_NOT_LOOPBACK).status_code == 403
    assert _broadcast(client, "explode").status_code == 400
    assert client.post("/api/layout/broadcast", json={"op": "open", "args": []}).status_code == 400
    assert client.post("/api/layout/broadcast", data="{", content_type="application/json").status_code == 400


def test_the_read_ops_answer_from_the_inventory_and_the_state_files(client: FlaskClient, app: Flask) -> None:
    shell = _shell(app)
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    client.post("/api/projects/alpha/tabs", json={"address": str(_TERMINAL_1)})
    shell.layouts.save_layout("alpha", "c1", layout_showing(_TERMINAL_1), TEST_NOW)
    shell.clients.record_report(
        ClientStateReport(client_id=ClientId("c1"), device_kind=DeviceKind.DESKTOP, active_view=ViewId("alpha")),
        TEST_NOW,
    )
    shell.activity.append_message("c1", "desktop", "alpha", "chat", "agent-1", "hello")
    _register_client(app, "c1", "alpha")

    views = _broadcast(client, "views").get_json()["views"]
    assert [view["id"] for view in views] == ["alpha", "everything"]
    assert views[0]["tabs"] == [str(_TERMINAL_1)] and views[0]["clients"] == [{"id": "c1", "device_kind": "desktop"}]
    assert views[1]["tabs"] == [str(_TERMINAL_1), str(_FILES)]

    listing = _broadcast(client, "list").get_json()
    assert listing["view_id"] == "alpha"
    assert listing["apps"][0]["instances"][0]["docked_in"] == ["c1"]
    assert _broadcast(client, "list", {"view": "Nowhere"}).status_code == 404

    inspected = _broadcast(client, "inspect", {"view": "Alpha"}).get_json()
    assert inspected["client_id"] == "c1"
    assert inspected["layout"]["panels"] == [
        {"address": str(_TERMINAL_1), "tab_id": "tab-0000000000000000", "title": "Terminal 1"}
    ]

    context = _broadcast(client, "context").get_json()["clients"]
    assert context[0]["client_id"] == "c1" and context[0]["is_connected"] is True
    assert context[0]["recent_messages"][0]["address"] == "app:chat?instance=agent-1"


def test_load_switches_the_requesting_agents_client(client: FlaskClient, app: Flask) -> None:
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    _shell(app).activity.append_message("c7", "desktop", "everything", "chat", "agent-1", "hello")
    client_queue = _register_client(app, "c7", "everything")

    assert _broadcast(client, "load").status_code == 400
    assert _broadcast(client, "load", {"view": "Nowhere"}).status_code == 404
    loaded = _broadcast(client, "load", {"view": "alpha"})
    assert loaded.status_code == 200 and loaded.get_json() == {
        "ok": True,
        "view_id": "alpha",
        "target_client_id": "c7",
    }
    assert drain_messages(client_queue) == [
        {"type": "load_layout", "view_id": "alpha", "display_name": "Alpha", "target_client_id": "c7"}
    ]


def test_mutating_ops_need_a_client_on_the_target_view(client: FlaskClient, app: Flask) -> None:
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    assert _broadcast(client, "open", {"address": str(_TERMINAL_1)}).status_code == 412

    everything_queue = _register_client(app, "c1", "everything")
    alpha_queue = _register_client(app, "c2", "alpha")
    assert _broadcast(client, "open", {"address": "terminal:terminal-1", "view": "alpha"}).status_code == 400
    assert _broadcast(client, "open", {"address": "app:nope", "view": "alpha"}).status_code == 404
    opened = _broadcast(client, "open", {"address": str(_TERMINAL_1), "view": "Alpha"})
    assert opened.status_code == 200
    assert drain_messages(alpha_queue) == [
        {"type": "layout_op", "op": "open", "args": {"address": str(_TERMINAL_1)}, "requester_agent_id": "agent-1"}
    ]
    assert drain_messages(everything_queue) == []

    # ``self`` is the requester's own chat, resolved by the client rather than parsed here.
    assert _broadcast(client, "focus", {"address": "self", "view": "alpha"}).status_code == 200
    assert drain_messages(alpha_queue) == [
        {"type": "layout_op", "op": "focus", "args": {"address": "self"}, "requester_agent_id": "agent-1"}
    ]

    # Two clients on different views and no ``view`` named: nothing to default to.
    assert _broadcast(client, "focus", {"address": str(_TERMINAL_1)}).status_code == 412
    # A refresh is not a mutation: it reaches every client and takes no mutex.
    assert _broadcast(client, "refresh", {"address": str(_FILES)}).status_code == 200
    assert [message["op"] for message in drain_messages(everything_queue)] == ["refresh"]
    assert [message["op"] for message in drain_messages(alpha_queue)] == ["refresh"]


def test_a_held_mutex_is_a_409_with_the_holder(client: FlaskClient, app: Flask) -> None:
    _register_client(app, "c1", "everything")
    assert _shell(app).layout_mutex.try_acquire("agent-9", "close", {"address": str(_FILES)}) is None
    conflict = _broadcast(client, "open", {"address": str(_FILES)})
    assert conflict.status_code == 409
    assert conflict.get_json()["in_flight"]["agent_id"] == "agent-9"
    assert conflict.get_json()["retry_after_ms"] > 0


def test_reload_system_interface_reaches_every_view_and_null_args_are_refused(client: FlaskClient, app: Flask) -> None:
    everything_queue = _register_client(app, "c1", "everything")
    alpha_queue = _register_client(app, "c2", "alpha")
    response = client.post("/api/layout/broadcast", json={"op": "reload_system_interface", "agent_id": "agent-1"})
    assert response.status_code == 200
    for client_queue in (everything_queue, alpha_queue):
        ops = [message["op"] for message in drain_messages(client_queue) if message["type"] == "layout_op"]
        assert ops == ["reload_system_interface"]
    assert client.post("/api/layout/broadcast", json={"op": "refresh", "args": None}).status_code == 400
