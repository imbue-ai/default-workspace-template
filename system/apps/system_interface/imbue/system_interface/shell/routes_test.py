"""Tests for the shell's HTTP routes (contracts.md sections 5 and 6) over a test state with a fake-fed inventory."""

import queue
from pathlib import Path
from typing import Any

import pytest
from app_instances.data_types import InstanceStatus
from app_instances.testing import StubInstanceSource
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.inventory import HttpInstanceFetcher
from imbue.system_interface.shell.liveness import probe_all_app_liveness
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.routes import _resolve_client
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.testing import FakeInstanceFetcher
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import TEST_TERMINAL_URL
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import layout_showing
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.testing import FakeSupervisorServer
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_TERMINAL_1 = Address("app:terminal?instance=terminal-1")
_TERMINAL_2 = Address("app:terminal?instance=terminal-2")
_FILES = Address("app:files")
_TAB = TabId("tab-000000000000000a")
_NOT_LOOPBACK = {"REMOTE_ADDR": "10.0.0.7"}


def _shell(app: Flask) -> ShellState:
    return state_of(app).shell


def _register_client(app: Flask, client_id: str, view_id: str) -> "queue.Queue[str | None]":
    """A connected window of ``client_id`` on ``view_id``, recorded the way its ``client_state`` report would record it."""
    client_queue = _shell(app).broadcaster.register()
    _shell(app).broadcaster.set_client_info(client_queue, client_id, view_id, "desktop")
    _shell(app).clients.record_report(
        ClientStateReport(
            client_id=ClientId(client_id),
            device_kind=DeviceKind.DESKTOP,
            active_view=ViewId(view_id),
        ),
        TEST_NOW,
    )
    return client_queue


def _record_client(app: Flask, client_id: str, view_id: str) -> None:
    """A client the shell knows from an earlier visit, with no window open now."""
    _shell(app).clients.record_report(
        ClientStateReport(
            client_id=ClientId(client_id),
            device_kind=DeviceKind.DESKTOP,
            active_view=ViewId(view_id),
        ),
        TEST_NOW,
    )


def _panel_addresses(layout: dict[str, Any]) -> list[str]:
    return [panel["address"] for panel in layout["panels"]]


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
    shell.layouts.save_browser_layout("alpha", "c1", layout_showing(_TERMINAL_1), None, TEST_NOW)
    shell.layouts.save_browser_layout("everything", "c2", layout_showing(_TERMINAL_1), None, TEST_NOW)
    fetcher.list(TEST_TERMINAL_URL, instance_record("terminal-1"), instance_record("terminal-2"))
    client_queue = _register_client(app, "c1", "alpha")

    response = client.post(
        "/api/tabs/tab-0000000000000000/instance",
        json={"app": "terminal", "key": "terminal-2"},
    )

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
    _shell(app).layouts.save_browser_layout("everything", "c1", layout_showing(_FILES), None, TEST_NOW)
    assert (
        client.post(
            "/api/tabs/tab-00000000000000ff/instance",
            json={"app": "terminal", "key": "k"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/tabs/tab-0000000000000000/instance",
            json={"app": "terminal", "key": "k"},
        ).status_code
        == 400
    )
    assert client.post("/api/tabs/nope/instance", json={"app": "terminal", "key": "k"}).status_code == 400
    assert client.post("/api/tabs/tab-0000000000000000/instance", json={"app": "terminal"}).status_code == 400
    assert (
        client.post(
            "/api/tabs/tab-0000000000000000/instance",
            json={"app": "files", "key": ""},
            environ_base=_NOT_LOOPBACK,
        ).status_code
        == 403
    )


def test_client_activity_is_appended_by_kind(client: FlaskClient, app: Flask) -> None:
    base = {"client_id": "c1", "device_kind": "desktop", "view_id": "everything"}
    assert (
        client.post(
            "/api/client-activity",
            json={
                **base,
                "kind": "message",
                "app": "chat",
                "key": "agent-1",
                "text": "hi",
            },
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/client-activity",
            json={**base, "kind": "view_switch", "from_view_id": "alpha"},
        ).status_code
        == 204
    )
    assert client.post("/api/client-activity", json={**base, "kind": "nope"}).status_code == 400
    assert (
        client.post(
            "/api/client-activity",
            json={**base, "kind": "message"},
            environ_base=_NOT_LOOPBACK,
        ).status_code
        == 403
    )
    events = _shell(app).activity.read_events()
    assert [(event["type"], event["client_id"]) for event in events] == [
        ("message", "c1"),
        ("view_switch", "c1"),
    ]
    assert events[0]["key"] == "agent-1" and events[1]["from_view_id"] == "alpha"


# ---------- section 6: the relay ----------


def test_instance_verbs_are_relayed_and_the_list_refetched(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    stub_source: StubInstanceSource,
    stub_app_url: str,
) -> None:
    stub_source.records.append(instance_record("stub-1"))
    inventory = build_inventory(
        write_registry(
            tmp_path / "apps.toml",
            registry_row_toml("stub", stub_app_url, True, actions=[("new", "New")]),
        ),
        broadcaster,
        fetcher=HttpInstanceFetcher(),
    )
    inventory.refetch_now("stub")
    client = shell_application(tmp_path, inventory, broadcaster).test_client()

    created = client.post("/api/apps/stub/instances", json={"action": "new", "params": {}})
    assert created.status_code == 201 and created.get_json()["instance"]["key"] == "stub-2"
    assert inventory.find_instance(Address("app:stub?instance=stub-2")) is not None
    renamed = client.post("/api/apps/stub/instances/stub-2/rename", json={"title": "Renamed"})
    assert renamed.status_code == 200
    found = inventory.find_instance(Address("app:stub?instance=stub-2"))
    assert found is not None and found[1].title == "Renamed"
    assert client.post("/api/apps/stub/instances/stub-2/location", json={"path": "/deeper"}).status_code == 200
    # Stop and start pass the app's answer through and refetch on success, like every other verb.
    assert client.post("/api/apps/stub/instances/stub-2/stop").status_code == 400
    stub_source.is_stoppable = True
    assert client.post("/api/apps/stub/instances/stub-2/stop").status_code == 200
    found_stopped = inventory.find_instance(Address("app:stub?instance=stub-2"))
    assert found_stopped is not None and found_stopped[1].status == InstanceStatus.STOPPED
    assert client.post("/api/apps/stub/instances/stub-2/start").status_code == 200
    found_started = inventory.find_instance(Address("app:stub?instance=stub-2"))
    assert found_started is not None and found_started[1].status == InstanceStatus.IDLE
    assert client.post("/api/apps/stub/instances/stub-9/start").status_code == 404
    assert client.post("/api/apps/stub/instances/stub-2/delete").status_code == 204
    assert inventory.find_instance(Address("app:stub?instance=stub-2")) is None
    assert client.post("/api/apps/stub/instances/stub-9/rename", json={"title": "x"}).status_code == 404
    assert client.post("/api/apps/unknown/instances", json={"action": "new", "params": {}}).status_code == 404
    # A key that fails the key rule is refused by the shell, before the app (which would say 404) is asked.
    assert client.post("/api/apps/stub/instances/-not-a-key/rename", json={"title": "x"}).status_code == 400


# ---------- section 6: stop and start ----------


def test_stop_and_start_drive_the_supervised_program(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    fake_supervisor: FakeSupervisorServer,
) -> None:
    fake_supervisor.statename_by_program["files"] = "RUNNING"
    fake_supervisor.statename_by_program["system_interface"] = "RUNNING"
    registry_path = write_two_app_registry(
        tmp_path,
        registry_row_toml(
            "system_interface",
            "http://localhost:8000",
            program="system_interface",
            is_critical=True,
        ),
        registry_row_toml("chat", "http://localhost:8000", True, program="system_interface"),
        registry_row_toml(
            "plain",
            "http://localhost:1",
        ),
    )
    inventory = build_inventory(registry_path, broadcaster, prober=probe_all_app_liveness)
    client = shell_application(tmp_path, inventory, broadcaster).test_client()

    stopped = client.post("/api/apps/files/stop")
    assert stopped.status_code == 200 and stopped.get_json() == {
        "name": "files",
        "is_running": False,
    }
    assert fake_supervisor.statename_by_program["files"] == "STOPPED"
    started = client.post("/api/apps/files/start")
    assert started.status_code == 200 and started.get_json() == {
        "name": "files",
        "is_running": True,
    }

    assert client.post("/api/apps/system_interface/stop").status_code == 400
    assert client.post("/api/apps/chat/stop").status_code == 400
    assert client.post("/api/apps/plain/stop").status_code == 400
    assert client.post("/api/apps/unknown/stop").status_code == 404


def test_an_unreachable_supervisord_is_a_502(
    tmp_path: Path, broadcaster: WebSocketBroadcaster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINDS_SUPERVISOR_SOCKET", str(tmp_path / "missing.sock"))
    client = shell_application(
        tmp_path,
        build_inventory(write_two_app_registry(tmp_path), broadcaster),
        broadcaster,
    ).test_client()
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
    _shell(app).layouts.save_browser_layout("alpha", "c1", layout_showing(_TERMINAL_1), None, TEST_NOW)

    settings = client.post(
        "/api/projects/alpha/settings",
        json={"name": "Alpha 2", "color": "#222222", "glyph": 2},
    )
    assert settings.status_code == 200 and settings.get_json()["name"] == "Alpha 2"
    assert (
        client.post(
            "/api/projects/everything/settings",
            json={"name": "x", "color": "#222222", "glyph": 2},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/projects/missing/settings",
            json={"name": "x", "color": "#222222", "glyph": 2},
        ).status_code
        == 404
    )

    added = client.post("/api/projects/alpha/tabs", json={"address": str(_TERMINAL_1)})
    assert added.status_code == 200 and added.get_json()["tabs"] == [str(_TERMINAL_1)]
    assert client.post("/api/projects/alpha/tabs", json={"address": "terminal:terminal-1"}).status_code == 400
    removed = client.post("/api/projects/alpha/tabs/remove", json={"address": str(_TERMINAL_1)})
    assert removed.status_code == 200 and removed.get_json()["tabs"] == []

    assert (
        client.post(
            "/api/projects/alpha/shortcuts",
            json={"app": "terminal", "action": "open", "mode": "new"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/projects/alpha/shortcuts",
            json={"app": "nope", "action": "open", "mode": "new"},
        ).status_code
        == 400
    )
    flipped = client.post(
        "/api/projects/alpha/shortcuts",
        json={"app": "terminal", "action": "new", "mode": "focus"},
    )
    assert flipped.status_code == 200
    assert flipped.get_json()["shortcuts"][0] == {
        "app": "terminal",
        "action": "new",
        "mode": "focus",
    }
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
    assert client.get("/api/layouts/everything?client=c1&device=tablet").status_code == 400
    empty = client.get("/api/layouts/everything?client=c1&device=mobile").get_json()
    assert empty == {
        "dockview": None,
        "tabs": {},
        "device_kind": "mobile",
        "updated_at": None,
    }

    client_queue = _register_client(app, "c1", "everything")
    body = {
        "client_id": "c1",
        "save_id": "save-0000000000000001",
        "device_kind": "desktop",
        "dockview": {"panels": {"p0": {}}},
        "tabs": {
            "p0": {
                "address": str(_TERMINAL_1),
                "tab_id": str(_TAB),
                "last_focused_ms": 5,
            }
        },
    }
    saved = client.post("/api/layouts/everything", json=body)
    assert saved.status_code == 200 and saved.get_json()["updated_at"] is not None
    assert client.post("/api/layouts/missing", json=body).status_code == 404
    assert client.post("/api/layouts/everything", json={"client_id": "c1"}).status_code == 400
    assert client.post("/api/layouts/everything", json={**body, "save_id": "nope"}).status_code == 400

    own = client.get("/api/layouts/everything?client=c1").get_json()
    assert own["tabs"]["p0"]["address"] == str(_TERMINAL_1) and own["updated_at"] == saved.get_json()["updated_at"]
    seeded = client.get("/api/layouts/everything?client=c2&device=desktop").get_json()
    assert seeded["tabs"] == own["tabs"]
    assert client.get("/api/layouts/everything?client=c2&device=mobile").get_json()["tabs"] == {}
    # The write was announced with the window's own save id; a save that changes nothing is not.
    updates = [message for message in drain_messages(client_queue) if message["type"] == "layout_updated"]
    assert updates == [
        {
            "type": "layout_updated",
            "view_id": "everything",
            "client_id": "c1",
            "save_id": "save-0000000000000001",
        }
    ]
    again = {
        **body,
        "save_id": "save-0000000000000002",
        "base_updated_at": own["updated_at"],
    }
    unchanged = client.post("/api/layouts/everything", json=again)
    assert unchanged.status_code == 200 and unchanged.get_json() == {"updated_at": None}
    assert drain_messages(client_queue) == []
    # A save based on an older arrangement than the stored one is refused, and one based on the stored one lands.
    stale = {**again, "base_updated_at": None, "tabs": {}, "dockview": None}
    assert client.post("/api/layouts/everything", json=stale).status_code == 409
    fresh = {**again, "tabs": {}, "dockview": None}
    assert client.post("/api/layouts/everything", json=fresh).status_code == 200
    assert client.get("/api/layouts/everything?client=c1").get_json()["tabs"] == {}


def test_clients_and_the_inventory_document_are_served(client: FlaskClient, app: Flask) -> None:
    shell = _shell(app)
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    _register_client(app, "c1", "alpha")
    _record_client(app, "c2", "everything")
    shell.layouts.save_browser_layout("alpha", "c1", layout_showing(_TERMINAL_1), None, TEST_NOW)

    clients = client.get("/api/clients").get_json()["clients"]
    assert [(entry["id"], entry["is_connected"]) for entry in clients] == [
        ("c1", True),
        ("c2", False),
    ]

    document = client.get("/api/inventory").get_json()
    assert [project["id"] for project in document["projects"]] == ["alpha"]
    assert document["everything"] == {
        "id": "everything",
        "tabs": [str(_TERMINAL_1), str(_FILES)],
    }
    assert [entry["name"] for entry in document["apps"]] == ["terminal", "files"]
    assert {entry["id"]: entry["docked"] for entry in document["clients"]} == {
        "c1": [str(_TERMINAL_1)],
        "c2": [],
    }
    assert document["clients"][0]["active_view"] == "alpha" and document["clients"][0]["is_connected"] is True


def test_a_recorded_client_reads_the_seed_of_its_own_device_kind(client: FlaskClient, app: Flask) -> None:
    """The ``device`` query names the seed only for a client the shell has no record of."""
    mobile_body = {
        "client_id": "m1",
        "save_id": "save-0000000000000001",
        "device_kind": "mobile",
        "dockview": {"panels": {"p0": {}}},
        "tabs": {"p0": {"address": str(_FILES), "tab_id": str(_TAB), "last_focused_ms": 0}},
    }
    assert client.post("/api/layouts/everything", json=mobile_body).status_code == 200
    _shell(app).clients.record_report(
        ClientStateReport(
            client_id=ClientId("c2"),
            device_kind=DeviceKind.MOBILE,
            active_view=ViewId("everything"),
        ),
        TEST_NOW,
    )

    seeded = client.get("/api/layouts/everything?client=c2&device=desktop").get_json()
    assert seeded["device_kind"] == "mobile" and seeded["tabs"]["p0"]["address"] == str(_FILES)


# ---------- the broadcast endpoint ----------


def _broadcast(
    client: FlaskClient,
    op: str,
    args: dict[str, Any] | None = None,
    agent_id: str = "agent-1",
) -> Any:
    """Post an op the way ``layout.py`` does from the chat of ``agent_id``."""
    requester = f"app:chat?instance={agent_id}"
    return client.post(
        "/api/layout/broadcast",
        json={"op": op, "args": args or {}, "requester": requester},
    )


def test_the_broadcast_endpoint_validates_its_input(client: FlaskClient) -> None:
    assert _broadcast(client, "context").status_code == 200
    assert client.post("/api/layout/broadcast", json={"op": "context"}, environ_base=_NOT_LOOPBACK).status_code == 403
    assert _broadcast(client, "explode").status_code == 400
    assert client.post("/api/layout/broadcast", json={"op": "open", "args": []}).status_code == 400
    assert client.post("/api/layout/broadcast", data="{", content_type="application/json").status_code == 400
    # A requester that is not an address is refused, not dropped: dropped, the op would lose its
    # attribution and ``self`` would be reported as unset although the caller sent one.
    refused = client.post("/api/layout/broadcast", json={"op": "context", "requester": "chat:agent-1"})
    assert refused.status_code == 400
    assert "requester" in refused.get_json()["detail"]
    for not_an_address in (7, 0, False, []):
        assert (
            client.post("/api/layout/broadcast", json={"op": "context", "requester": not_an_address}).status_code
            == 400
        )


def test_the_read_ops_answer_from_the_state_files_and_the_activity_log(client: FlaskClient, app: Flask) -> None:
    shell = _shell(app)
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    client.post("/api/projects/alpha/tabs", json={"address": str(_TERMINAL_1)})
    shell.layouts.save_browser_layout("alpha", "c1", layout_showing(_TERMINAL_1), None, TEST_NOW)
    shell.clients.record_report(
        ClientStateReport(
            client_id=ClientId("c1"),
            device_kind=DeviceKind.DESKTOP,
            active_view=ViewId("alpha"),
        ),
        TEST_NOW,
    )
    shell.activity.append_message("c1", "desktop", "alpha", "chat", "agent-1", "hello")
    _register_client(app, "c1", "alpha")
    # A second client that has connected and done nothing else: it has no event in the log.
    _register_client(app, "c9", "everything")

    # The script's list and views read GET /api/inventory; the op route's reads are inspect and context.
    assert _broadcast(client, "list").status_code == 400
    assert _broadcast(client, "views").status_code == 400

    inspected = _broadcast(client, "inspect", {"view": "Alpha"}).get_json()
    assert inspected["client_id"] == "c1"
    assert inspected["layout"]["panels"] == [
        {
            "address": str(_TERMINAL_1),
            "tab_id": "tab-0000000000000000",
            "title": "Terminal 1",
        }
    ]

    context = _broadcast(client, "context").get_json()["clients"]
    assert [entry["client_id"] for entry in context] == ["c1", "c9"]
    assert context[0]["is_connected"] is True and context[0]["active_view"] == "alpha"
    assert context[0]["recent_messages"][0]["address"] == "app:chat?instance=agent-1"
    assert context[1] == {
        "client_id": "c9",
        "device_kind": "desktop",
        "active_view": "everything",
        "last_seen": "",
        "is_connected": True,
        "recent_messages": [],
    }


def test_an_op_is_attributed_to_the_client_that_last_messaged_the_requesting_agent(
    client: FlaskClient, app: Flask
) -> None:
    """With several clients on the view, the requester's own client is the one that last messaged its chat;
    an explicit ``client`` outranks that, and with neither there is no client to answer for."""
    shell = _shell(app)
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    _register_client(app, "c1", "alpha")
    _register_client(app, "c7", "alpha")
    shell.layouts.save_browser_layout("alpha", "c7", layout_showing(_TERMINAL_1), None, TEST_NOW)

    assert _broadcast(client, "inspect", {"view": "alpha"}).get_json()["client_id"] is None

    shell.activity.append_message("c7", "desktop", "alpha", "chat", "agent-1", "hello")
    attributed = _broadcast(client, "inspect", {"view": "alpha"}).get_json()
    assert attributed["client_id"] == "c7"
    assert [panel["address"] for panel in attributed["layout"]["panels"]] == [str(_TERMINAL_1)]
    assert _broadcast(client, "inspect", {"view": "alpha"}, agent_id="agent-2").get_json()["client_id"] is None
    assert _broadcast(client, "inspect", {"view": "alpha", "client": "c1"}).get_json()["client_id"] == "c1"
    # A client id names a layout file, so one outside the id's alphabet is refused before any read.
    assert _broadcast(client, "inspect", {"view": "alpha", "client": "../c1"}).status_code == 400


def test_a_bare_app_requester_is_attributed_to_no_client(app: Flask) -> None:
    """A requester that names an app and no instance has no client that last messaged it: the log is not
    searched under a made-up key."""
    shell = _shell(app)
    _register_client(app, "c7", "alpha")
    shell.activity.append_message("c7", "desktop", "alpha", "files", "None", "hello")
    _register_client(app, "c1", "alpha")

    assert _resolve_client(shell, {}, Address("app:files")) is None


def test_load_switches_the_requesting_agents_client(client: FlaskClient, app: Flask) -> None:
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    _shell(app).activity.append_message("c7", "desktop", "everything", "chat", "agent-1", "hello")
    client_queue = _register_client(app, "c7", "everything")
    _register_client(app, "c8", "everything")

    assert _broadcast(client, "load").status_code == 400
    assert _broadcast(client, "load", {"view": "Nowhere"}).status_code == 404
    loaded = _broadcast(client, "load", {"view": "alpha"})
    assert loaded.status_code == 200 and loaded.get_json() == {
        "ok": True,
        "view_id": "alpha",
        "target_client_id": "c7",
    }
    assert drain_messages(client_queue) == [{"type": "active_view_changed", "client_id": "c7", "view_id": "alpha"}]
    moved = _shell(app).clients.get_client("c7")
    assert moved is not None and str(moved.active_view) == "alpha"
    # A load onto the view the client already has moves nothing and says nothing.
    assert _broadcast(client, "load", {"view": "alpha"}).status_code == 200
    assert drain_messages(client_queue) == []
    # Two clients connected and an agent nobody messaged: nothing to switch, never everyone.
    assert _broadcast(client, "load", {"view": "alpha"}, agent_id="agent-2").status_code == 412
    assert _broadcast(client, "load", {"view": "alpha", "client": "nobody"}, agent_id="agent-2").status_code == 404
    explicit = _broadcast(client, "load", {"view": "alpha", "client": "c8"}, agent_id="agent-2")
    assert explicit.status_code == 200 and explicit.get_json()["target_client_id"] == "c8"


def test_document_ops_edit_the_target_clients_file_and_announce_the_write(client: FlaskClient, app: Flask) -> None:
    shell = _shell(app)
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    client_queue = _register_client(app, "c1", "alpha")

    assert _broadcast(client, "open", {"address": "terminal:terminal-1"}).status_code == 400
    assert _broadcast(client, "open", {"address": "app:nope"}).status_code == 404
    assert _broadcast(client, "open", {"address": "app:terminal?instance=terminal-9"}).status_code == 404

    opened = _broadcast(client, "open", {"address": str(_TERMINAL_1)})
    assert opened.status_code == 200
    answer = opened.get_json()
    assert (answer["view_id"], answer["client_id"], answer["created_address"]) == (
        "alpha",
        "c1",
        None,
    )
    assert _panel_addresses(answer["layout"]) == [str(_TERMINAL_1)]
    # The file is the truth: written for this client, filed into the project, and announced with a shell-minted id.
    stored = shell.layouts.read_client_layout("alpha", "c1")
    assert stored is not None and [str(tab.address) for tab in stored.tabs.values()] == [str(_TERMINAL_1)]
    assert stored.dockview is not None and stored.dockview["grid"]["root"]["type"] == "branch"
    assert shell.projects.get_project("alpha").tabs == (_TERMINAL_1,)
    messages = drain_messages(client_queue)
    updates = [message for message in messages if message["type"] == "layout_updated"]
    assert len(updates) == 1 and updates[0]["client_id"] == "c1" and updates[0]["view_id"] == "alpha"
    assert updates[0]["save_id"].startswith("save-")
    assert "projects_updated" in [message["type"] for message in messages]
    # Opening an address the arrangement already shows focuses it rather than docking it twice; since that panel
    # is the active one already, nothing is written or announced.
    assert _panel_addresses(_broadcast(client, "open", {"address": str(_TERMINAL_1)}).get_json()["layout"]) == [
        str(_TERMINAL_1)
    ]
    assert shell.layouts.read_client_layout("alpha", "c1") == stored
    assert [message["type"] for message in drain_messages(client_queue) if message["type"] == "layout_updated"] == []

    split = _broadcast(
        client,
        "split",
        {
            "address": str(_FILES),
            "relative_to": str(_TERMINAL_1),
            "direction": "below",
            "ratio": 0.5,
        },
    )
    assert split.status_code == 200
    tree = split.get_json()["layout"]["tree"]
    assert tree["type"] == "branch" and tree["children"][0]["type"] == "branch"
    assert [leaf["panels"][0]["address"] for leaf in tree["children"][0]["children"]] == [
        str(_TERMINAL_1),
        str(_FILES),
    ]
    bad_anchor = _broadcast(client, "split", {"address": str(_TERMINAL_2), "relative_to": "app:nope"})
    assert bad_anchor.status_code == 404 and "app:nope" in bad_anchor.get_json()["detail"]
    assert _broadcast(client, "split", {"address": str(_FILES), "direction": "sideways"}).status_code == 400

    focused = _broadcast(client, "focus", {"address": str(_TERMINAL_1)})
    assert focused.status_code == 200
    focused_leaf = focused.get_json()["layout"]["tree"]["children"][0]["children"][0]
    assert focused_leaf["panels"][0]["active"] is True
    assert _broadcast(client, "focus", {"address": "app:browser?instance=x"}).status_code == 404

    moved = _broadcast(
        client,
        "move",
        {
            "address": str(_FILES),
            "relative_to": str(_TERMINAL_1),
            "direction": "within",
        },
    )
    assert moved.status_code == 200
    assert [panel["address"] for panel in moved.get_json()["layout"]["tree"]["children"][0]["panels"]] == [
        str(_TERMINAL_1),
        str(_FILES),
    ]

    closed = _broadcast(client, "close", {"address": str(_FILES)})
    assert closed.status_code == 200 and _panel_addresses(closed.get_json()["layout"]) == [str(_TERMINAL_1)]
    assert _broadcast(client, "close", {"address": str(_FILES)}).status_code == 404
    emptied = _broadcast(client, "close", {"address": str(_TERMINAL_1)})
    assert emptied.status_code == 200 and emptied.get_json()["layout"] == {
        "active_panel": None,
        "panels": [],
        "tree": None,
    }
    # Closing changes no tab set.
    assert shell.projects.get_project("alpha").tabs == (_TERMINAL_1, _FILES)


def test_self_names_the_requesters_own_docked_instance(client: FlaskClient, app: Flask) -> None:
    """``self`` is the requester's own instance, read from the op's ``requester``: where ``open`` lands, the target of
    any addressed op, and the anchor a split or a move defaults to. An op that carried no requester cannot mean it."""
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    _register_client(app, "c1", "alpha")

    def as_terminal_1(op: str, args: dict[str, Any]) -> Any:
        return client.post("/api/layout/broadcast", json={"op": op, "args": args, "requester": str(_TERMINAL_1)})

    def leaf_addresses(layout: dict[str, Any]) -> list[list[str]]:
        tree = layout["tree"]
        leaves = tree["children"] if tree["type"] == "branch" else [tree]
        return [[panel["address"] for panel in leaf["panels"]] for leaf in leaves]

    assert as_terminal_1("open", {"address": str(_TERMINAL_1)}).status_code == 200
    # An open lands beside the requester's own docked instance.
    opened = as_terminal_1("open", {"address": str(_FILES)})
    assert opened.status_code == 200
    assert leaf_addresses(opened.get_json()["layout"]) == [[str(_TERMINAL_1)], [str(_FILES)]]

    unattributed = client.post("/api/layout/broadcast", json={"op": "focus", "args": {"address": "self"}})
    assert unattributed.status_code == 400 and "requester" in unattributed.get_json()["detail"]

    focused = as_terminal_1("focus", {"address": "self"})
    assert focused.status_code == 200
    assert focused.get_json()["layout"]["active_panel"] != opened.get_json()["layout"]["active_panel"]

    # A move with no anchor is relative to self: within its group tabs beside it.
    moved = as_terminal_1("move", {"address": str(_FILES), "direction": "within"})
    assert moved.status_code == 200
    assert leaf_addresses(moved.get_json()["layout"]) == [[str(_TERMINAL_1), str(_FILES)]]


def test_an_op_lands_with_no_browser_connected_and_never_on_a_guessed_client(client: FlaskClient, app: Flask) -> None:
    shell = _shell(app)
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    # Nothing connected and nothing recorded: there is no client to arrange for.
    assert _broadcast(client, "open", {"address": str(_TERMINAL_1)}).status_code == 412
    # A recorded client with no window open is a fine target when named.
    _record_client(app, "c1", "alpha")
    landed = _broadcast(client, "open", {"address": str(_TERMINAL_1), "client": "c1"})
    assert landed.status_code == 200 and _panel_addresses(landed.get_json()["layout"]) == [str(_TERMINAL_1)]
    assert shell.layouts.read_client_layout("alpha", "c1") is not None
    assert _broadcast(client, "open", {"address": str(_TERMINAL_1), "client": "nobody"}).status_code == 404
    # Two clients connected and no attribution: refused with the clients listed, never applied to both.
    _register_client(app, "c2", "alpha")
    _register_client(app, "c3", "everything")
    refused = _broadcast(client, "open", {"address": str(_FILES)}, agent_id="agent-2")
    assert (
        refused.status_code == 412
        and "c2" in refused.get_json()["detail"]
        and "--client" in refused.get_json()["detail"]
    )
    assert shell.layouts.read_client_layout("alpha", "c2") is None
    # The client that last messaged the requesting agent is the one the op is for.
    shell.activity.append_message("c3", "desktop", "everything", "chat", "agent-2", "hello")
    attributed = _broadcast(client, "open", {"address": str(_FILES)}, agent_id="agent-2")
    assert attributed.status_code == 200 and attributed.get_json()["client_id"] == "c3"
    assert shell.layouts.read_client_layout("everything", "c3") is not None


def test_view_edits_that_views_file_and_switches_the_client_to_it(client: FlaskClient, app: Flask) -> None:
    shell = _shell(app)
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    client_queue = _register_client(app, "c1", "everything")

    assert _broadcast(client, "open", {"address": str(_TERMINAL_1), "view": "Nowhere"}).status_code == 404
    opened = _broadcast(client, "open", {"address": str(_TERMINAL_1), "view": "Alpha"})
    assert opened.status_code == 200 and opened.get_json()["view_id"] == "alpha"
    assert shell.layouts.read_client_layout("alpha", "c1") is not None
    assert shell.layouts.read_client_layout("everything", "c1") is None
    switched = shell.clients.get_client("c1")
    assert switched is not None and str(switched.active_view) == "alpha"
    types = [message["type"] for message in drain_messages(client_queue)]
    assert "layout_updated" in types and "active_view_changed" in types
    # A client that never visited the view starts from its seed, which another client saved.
    shell.layouts.save_browser_layout("alpha", "seed-maker", layout_showing(_FILES), None, TEST_NOW)
    _record_client(app, "c2", "everything")
    inherited = _broadcast(client, "open", {"address": str(_TERMINAL_1), "view": "alpha", "client": "c2"})
    assert _panel_addresses(inherited.get_json()["layout"]) == [
        str(_FILES),
        str(_TERMINAL_1),
    ]


def test_open_of_a_bare_app_creates_through_the_relay_inside_the_op(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    stub_source: StubInstanceSource,
    stub_app_url: str,
) -> None:
    inventory = build_inventory(
        write_registry(
            tmp_path / "apps.toml",
            registry_row_toml("stub", stub_app_url, True, actions=[("new", "New"), ("other", "Other")]),
        ),
        broadcaster,
        fetcher=HttpInstanceFetcher(),
    )
    inventory.refetch_now("stub")
    app = shell_application(tmp_path, inventory, broadcaster)
    client = app.test_client()
    _register_client(app, "c1", "everything")

    created = _broadcast(
        client,
        "open",
        {"address": "app:stub", "action": "new", "params": {"path": "/x"}},
    )
    assert created.status_code == 200
    assert created.get_json()["created_address"] == "app:stub?instance=stub-1"
    assert _panel_addresses(created.get_json()["layout"]) == ["app:stub?instance=stub-1"]
    assert [(str(record.key), record.title, str(record.url)) for record in stub_source.records] == [
        ("stub-1", "Stub 1", "/x")
    ]
    assert "create:new:{'path': '/x'}" in stub_source.calls
    # An action the app does not declare is the app's own 400, passed through.
    assert _broadcast(client, "open", {"address": "app:stub", "action": "other"}).status_code == 400
    # A split of a bare app creates too, beside its anchor.
    split = _broadcast(
        client,
        "split",
        {"address": "app:stub", "relative_to": "app:stub?instance=stub-1"},
    )
    assert split.status_code == 200 and split.get_json()["created_address"] == "app:stub?instance=stub-2"
    # The app's refusal reaches the caller as the op's error, and nothing is docked.
    assert _broadcast(client, "open", {"address": "app:stub", "action": "nope"}).status_code == 400
    stub_source.is_ready = False
    assert _broadcast(client, "open", {"address": "app:stub"}).status_code == 503
    stored = _shell(app).layouts.read_client_layout("everything", "c1")
    assert stored is not None and len(stored.tabs) == 2


def test_transient_ops_reach_the_target_clients_windows(client: FlaskClient, app: Flask) -> None:
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    first_window = _register_client(app, "c1", "alpha")
    second_window = _register_client(app, "c1", "everything")
    other_client = _register_client(app, "c2", "alpha")
    _shell(app).activity.append_message("c1", "desktop", "alpha", "chat", "agent-1", "hello")

    assert _broadcast(client, "maximize", {"address": "terminal:terminal-1"}).status_code == 400
    assert _broadcast(client, "maximize", {"address": "app:nope"}).status_code == 404
    maximized = _broadcast(client, "maximize", {"address": str(_TERMINAL_1)})
    assert maximized.status_code == 200 and maximized.get_json()["target_client_id"] == "c1"
    for window in (first_window, second_window):
        assert drain_messages(window) == [
            {
                "type": "layout_op",
                "op": "maximize",
                "args": {"address": str(_TERMINAL_1)},
                "requester": "app:chat?instance=agent-1",
                "target_client_id": "c1",
            }
        ]
    assert drain_messages(other_client) == []
    # A refresh of a whole app, like the interface reload, reaches every window of every client.
    refreshed = _broadcast(client, "refresh", {"address": str(_FILES)}, agent_id="agent-9")
    assert refreshed.status_code == 200 and refreshed.get_json()["target_client_id"] is None
    for window in (first_window, second_window, other_client):
        assert [message["op"] for message in drain_messages(window)] == ["refresh"]
    # A refresh of one instance is that client's, and an agent nobody messaged has no client.
    assert _broadcast(client, "refresh", {"address": str(_TERMINAL_1)}, agent_id="agent-9").status_code == 412


def test_reload_system_interface_reaches_every_view_and_null_args_are_refused(client: FlaskClient, app: Flask) -> None:
    everything_queue = _register_client(app, "c1", "everything")
    alpha_queue = _register_client(app, "c2", "alpha")
    response = client.post("/api/layout/broadcast", json={"op": "reload_system_interface"})
    assert response.status_code == 200
    for client_queue in (everything_queue, alpha_queue):
        reloads = [message for message in drain_messages(client_queue) if message["type"] == "layout_op"]
        assert [message["op"] for message in reloads] == ["reload_system_interface"]
        assert reloads[0]["target_client_id"] is None
    assert client.post("/api/layout/broadcast", json={"op": "refresh", "args": None}).status_code == 400
