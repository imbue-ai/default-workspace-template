"""Tests for the shell's HTTP routes (contracts.md sections 5 and 6) over a test state with a fake-fed inventory."""

import queue
from pathlib import Path
from typing import Any

import pytest
from app_instances.data_types import InstanceStatus
from app_instances.testing import StubInstanceSource
from app_manifest.primitives import AppName
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import instance_panel_params_json
from imbue.system_interface.shell.inventory import HttpInstanceFetcher
from imbue.system_interface.shell.layout_ops import OpRequester
from imbue.system_interface.shell.liveness import probe_all_app_liveness
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.route_helpers import resolve_client
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.testing import FakeInstanceFetcher
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import TEST_TERMINAL_URL
from imbue.system_interface.shell.testing import addresses_by_panel_id
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import layout_showing
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.shell.wallpapers import BUNDLED_WALLPAPERS_DIRNAME
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
    _shell(app).broadcaster.set_client_info(client_queue, client_id, view_id, "desktop", active_desktop="")
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


# Section 5


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
    alpha = shell.layouts.read_layout("alpha", "c1", DeviceKind.DESKTOP)
    everything = shell.layouts.read_layout("everything", "c2", DeviceKind.DESKTOP)
    assert list(addresses_by_panel_id(alpha.dockview).values()) == [_TERMINAL_2]
    assert list(addresses_by_panel_id(everything.dockview).values()) == [_TERMINAL_2]
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


# Section 6: the relay


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


def test_a_relayed_delete_drops_the_instance_from_every_tab_set_and_layout(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    stub_source: StubInstanceSource,
    stub_app_url: str,
) -> None:
    stub_1 = Address("app:stub?instance=stub-1")
    stub_2 = Address("app:stub?instance=stub-2")
    stub_source.records.extend([instance_record("stub-1"), instance_record("stub-2")])
    inventory = build_inventory(
        write_registry(tmp_path / "apps.toml", registry_row_toml("stub", stub_app_url, True)),
        broadcaster,
        fetcher=HttpInstanceFetcher(),
    )
    inventory.refetch_now("stub")
    app = shell_application(tmp_path, inventory, broadcaster)
    shell = _shell(app)
    shell.projects.create_project("Alpha", "#111111", 0, ())
    shell.projects.add_tab("alpha", stub_1)
    shell.projects.add_tab("alpha", stub_2)
    shell.layouts.save_browser_layout("alpha", "c1", layout_showing(stub_1, stub_2), None, TEST_NOW)
    client_queue = broadcaster.register()

    assert app.test_client().post("/api/apps/stub/instances/stub-1/delete").status_code == 204

    assert shell.projects.get_project("alpha").tabs == (stub_2,)
    remaining = shell.layouts.read_layout("alpha", "c1", DeviceKind.DESKTOP)
    assert addresses_by_panel_id(remaining.dockview) == {"p1": stub_2}
    types = [message["type"] for message in drain_messages(client_queue)]
    assert "projects_updated" in types and "layout_updated" in types


def test_a_refused_delete_keeps_the_instance_in_its_tab_sets(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    stub_source: StubInstanceSource,
    stub_app_url: str,
) -> None:
    stub_1 = Address("app:stub?instance=stub-1")
    stub_source.records.append(instance_record("stub-1"))
    inventory = build_inventory(
        write_registry(tmp_path / "apps.toml", registry_row_toml("stub", stub_app_url, True)),
        broadcaster,
        fetcher=HttpInstanceFetcher(),
    )
    inventory.refetch_now("stub")
    app = shell_application(tmp_path, inventory, broadcaster)
    shell = _shell(app)
    shell.projects.create_project("Alpha", "#111111", 0, ())
    shell.projects.add_tab("alpha", stub_1)

    stub_source.is_ready = False

    assert app.test_client().post("/api/apps/stub/instances/stub-1/delete").status_code >= 400

    assert shell.projects.get_project("alpha").tabs == (stub_1,)


# Section 6: stop and start


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


# Section 6: projects


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


# Section 6: layouts


def test_layouts_are_read_per_client_with_the_seed_as_fallback(client: FlaskClient, app: Flask) -> None:
    assert client.get("/api/layouts/missing?client=c1").status_code == 404
    assert client.get("/api/layouts/everything").status_code == 400
    assert client.get("/api/layouts/everything?client=c1&device=tablet").status_code == 400
    empty = client.get("/api/layouts/everything?client=c1&device=mobile").get_json()
    assert empty == {
        "dockview": None,
        "device_kind": "mobile",
        "updated_at": None,
    }

    client_queue = _register_client(app, "c1", "everything")
    body = {
        "client_id": "c1",
        "save_id": "save-0000000000000001",
        "device_kind": "desktop",
        "dockview": {"panels": {"p0": {"params": instance_panel_params_json(_TERMINAL_1, _TAB, 5)}}},
    }
    saved = client.post("/api/layouts/everything", json=body)
    assert saved.status_code == 200 and saved.get_json()["updated_at"] is not None
    assert client.post("/api/layouts/missing", json=body).status_code == 404
    assert client.post("/api/layouts/everything", json={"client_id": "c1"}).status_code == 400
    assert client.post("/api/layouts/everything", json={**body, "save_id": "nope"}).status_code == 400

    own = client.get("/api/layouts/everything?client=c1").get_json()
    assert list(addresses_by_panel_id(own["dockview"]).values()) == [_TERMINAL_1]
    assert "tabs" not in own and own["updated_at"] == saved.get_json()["updated_at"]
    seeded = client.get("/api/layouts/everything?client=c2&device=desktop").get_json()
    assert seeded["dockview"] == own["dockview"]
    assert client.get("/api/layouts/everything?client=c2&device=mobile").get_json()["dockview"] is None
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
    stale = {**again, "base_updated_at": None, "dockview": None}
    assert client.post("/api/layouts/everything", json=stale).status_code == 409
    fresh = {**again, "dockview": None}
    assert client.post("/api/layouts/everything", json=fresh).status_code == 200
    assert client.get("/api/layouts/everything?client=c1").get_json()["dockview"] is None


def test_a_save_in_the_older_shape_is_folded_into_the_panels_params(client: FlaskClient, app: Flask) -> None:
    """A window still running the bundle from before params-only layouts posts a ``tabs`` block; the shell keeps its meaning."""
    body = {
        "client_id": "c1",
        "save_id": "save-0000000000000001",
        "device_kind": "desktop",
        "dockview": {
            "panels": {"p0": {"params": {"kind": "instance", "address": "app:stale", "tabId": "tab-0000000000000000"}}}
        },
        "tabs": {"p0": {"address": str(_TERMINAL_1), "tab_id": str(_TAB), "last_focused_ms": 5}},
    }
    assert client.post("/api/layouts/everything", json=body).status_code == 200
    own = client.get("/api/layouts/everything?client=c1").get_json()
    assert "tabs" not in own
    assert own["dockview"]["panels"]["p0"]["params"] == {
        "kind": "instance",
        "address": str(_TERMINAL_1),
        "tabId": str(_TAB),
        "lastFocusedMs": 5,
    }


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
        "dockview": {"panels": {"p0": {"params": instance_panel_params_json(_FILES, _TAB, 0)}}},
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
    assert seeded["device_kind"] == "mobile" and list(addresses_by_panel_id(seeded["dockview"]).values()) == [_FILES]


# The broadcast endpoint


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

    # The script's list and views read GET /api/inventory; the address route's reads are inspect and context
    # (``list`` is the desktop vocabulary's, answered from the desktops).
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
        "active_desktop": None,
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

    assert resolve_client(shell, {}, OpRequester(app=AppName("files"), marker="")) is None


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
    assert stored is not None and list(addresses_by_panel_id(stored.dockview).values()) == [_TERMINAL_1]
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


def test_an_open_with_no_docked_anchor_tabs_into_the_active_group(
    client: FlaskClient, app: Flask, fetcher: FakeInstanceFetcher
) -> None:
    """An ``open`` with nothing to be beside -- the auto-open reactor, or any agent surfacing its own
    chat, which is never docked yet -- lands where the user is looking. Docking it *beside* the active
    group instead split a fresh column open every time that group was the rightmost, which it usually
    is, and each such open left its own group active for the next one to split beside."""
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    fetcher.list(TEST_TERMINAL_URL, instance_record("terminal-1"), instance_record("terminal-2"))
    _shell(app).inventory.refetch_now("terminal")
    _register_client(app, "c1", "alpha")

    def leaf_addresses(layout: dict[str, Any]) -> list[list[str]]:
        tree = layout["tree"]
        leaves = tree["children"] if tree["type"] == "branch" else [tree]
        return [[panel["address"] for panel in leaf["panels"]] for leaf in leaves]

    def as_terminal_1(op: str, args: dict[str, Any]) -> Any:
        return client.post("/api/layout/broadcast", json={"op": op, "args": args, "requester": str(_TERMINAL_1)})

    # Two groups, the right-hand one active: an anchored open still splits beside its docked requester.
    assert as_terminal_1("open", {"address": str(_TERMINAL_1)}).status_code == 200
    anchored = as_terminal_1("open", {"address": str(_FILES)})
    assert leaf_addresses(anchored.get_json()["layout"]) == [[str(_TERMINAL_1)], [str(_FILES)]]

    # The reactor's own op: no requester at all, so no anchor to be beside.
    unanchored = client.post(
        "/api/layout/broadcast", json={"op": "open", "args": {"address": str(_TERMINAL_2)}, "requester": ""}
    )
    assert unanchored.status_code == 200
    assert leaf_addresses(unanchored.get_json()["layout"]) == [[str(_TERMINAL_1)], [str(_FILES), str(_TERMINAL_2)]]


def test_an_unanchored_open_still_splits_when_it_asks_for_a_new_group(client: FlaskClient, app: Flask) -> None:
    """``new_group`` is the caller saying it wants a column of its own, and ``_dock`` reads a ``within``
    direction ahead of that flag -- so the unanchored default must not swallow the flag."""
    client.post("/api/projects", json={"name": "Alpha", "color": "#111111", "glyph": 1})
    _register_client(app, "c1", "alpha")

    assert _broadcast(client, "open", {"address": str(_TERMINAL_1)}).status_code == 200
    split = _broadcast(client, "open", {"address": str(_FILES), "new_group": True})

    assert split.status_code == 200
    tree = split.get_json()["layout"]["tree"]
    assert tree["type"] == "branch" and len(tree["children"]) == 2


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
    assert stored is not None and len(addresses_by_panel_id(stored.dockview)) == 2


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


# The desktop interface (desktop contracts.md sections 5 and 8)


def _register_desktop_client(app: Flask, client_id: str, desktop_id: str) -> "queue.Queue[str | None]":
    """A connected window of ``client_id`` on ``desktop_id``, recorded the way the desktop shell's report records it."""
    client_queue = _shell(app).broadcaster.register()
    _shell(app).broadcaster.set_client_info(client_queue, client_id, "", "desktop", active_desktop=desktop_id)
    _shell(app).record_client_report(
        ClientStateReport(client_id=ClientId(client_id), active_desktop=DesktopId(desktop_id))
    )
    return client_queue


def _open_window(client: FlaskClient, app_name: str, path: str, client_id: str = "c1", **extra: Any) -> Any:
    return client.post(
        "/api/desktops/home/windows", json={"app": app_name, "path": path, "client_id": client_id, **extra}
    )


def _placements(client: FlaskClient, client_id: str) -> list[dict[str, Any]]:
    return client.get(f"/api/placements/home?client={client_id}").get_json()["placements"]


def _desktop_windows(client: FlaskClient) -> list[dict[str, Any]]:
    (home,) = client.get("/api/desktops").get_json()["desktops"]
    return home["windows"]


def test_the_default_desktop_is_seeded_from_the_registry_on_the_first_read(client: FlaskClient) -> None:
    desktops = client.get("/api/desktops").get_json()["desktops"]
    assert [desktop["id"] for desktop in desktops] == ["home"]
    (home,) = desktops
    assert home["name"] == "Home" and home["sharing"] == "shared" and home["wallpaper"] is None
    assert home["windows"] == []
    assert home["shortcuts"] == [
        {
            "target": {"kind": "launch", "app": "terminal", "launch": "new"},
            "mode": "new",
            "cell": {"column": 0, "row": 0},
        },
        {
            "target": {"kind": "launch", "app": "files", "launch": "open"},
            "mode": "focus",
            "cell": {"column": 0, "row": 1},
        },
    ]


def test_desktops_are_created_settled_papered_and_deleted(client: FlaskClient, app: Flask) -> None:
    shell = _shell(app)
    client_queue = _register_desktop_client(app, "c1", "home")
    created = client.post("/api/desktops", json={"name": "Research", "color": "#12B5A5", "glyph": 4})
    assert created.status_code == 201
    assert created.get_json()["id"] == "research" and len(created.get_json()["shortcuts"]) == 2
    assert client.post("/api/desktops", json={"name": "research!", "color": "#12B5A5", "glyph": 4}).status_code == 409
    assert client.post("/api/desktops", json={"name": "Bad", "color": "red", "glyph": 4}).status_code == 400
    settled = client.post(
        "/api/desktops/research/settings",
        json={"name": "Research 2", "color": "#222222", "glyph": 2, "sharing": "personal"},
    )
    assert settled.status_code == 200 and settled.get_json()["sharing"] == "personal"
    assert (
        client.post(
            "/api/desktops/missing/settings", json={"name": "x", "color": "#222222", "glyph": 2, "sharing": "shared"}
        ).status_code
        == 404
    )

    # A wallpaper must exist to be set; file wallpapers are whatever sits in the wallpapers directory.
    unknown = client.post("/api/desktops/research/wallpaper", json={"wallpaper": {"kind": "bundled", "name": "nope"}})
    assert unknown.status_code == 404
    shell.wallpaper_files_directory.mkdir(parents=True)
    (shell.wallpaper_files_directory / "mine.png").write_bytes(b"png")
    bundled_directory = state_of(app).static_directory / BUNDLED_WALLPAPERS_DIRNAME
    bundled_directory.mkdir(parents=True)
    (bundled_directory / "dawn.png").write_bytes(b"png")
    # The bundled wallpapers beside the frontend bundle come first, then the wallpapers directory's files.
    assert client.get("/api/wallpapers").get_json() == {
        "wallpapers": [
            {"kind": "bundled", "name": "dawn", "url": "/wallpapers/bundled/dawn"},
            {"kind": "file", "name": "mine", "url": "/wallpapers/file/mine"},
        ]
    }
    papered = client.post("/api/desktops/research/wallpaper", json={"wallpaper": {"kind": "file", "name": "mine"}})
    assert papered.status_code == 200 and papered.get_json()["wallpaper"] == {"kind": "file", "name": "mine"}
    assert client.get("/wallpapers/file/mine").status_code == 200
    assert client.get("/wallpapers/bundled/dawn").status_code == 200
    missing = client.get("/wallpapers/file/nope")
    assert missing.status_code == 404 and "nope" in missing.get_json()["detail"]
    assert client.get("/wallpapers/odd/mine").status_code == 404
    assert client.post("/api/desktops/research/wallpaper", json={"wallpaper": None}).get_json()["wallpaper"] is None

    # Shortcuts: a launch path the app does not declare is refused; set replaces in place; move displaces.
    bad_shortcut = {
        "target": {"kind": "launch", "app": "terminal", "launch": "open"},
        "mode": "new",
        "cell": {"column": 1, "row": 1},
    }
    assert client.post("/api/desktops/research/shortcuts", json=bad_shortcut).status_code == 400
    flipped = client.post(
        "/api/desktops/research/shortcuts",
        json={
            "target": {"kind": "launch", "app": "terminal", "launch": "new"},
            "mode": "focus",
            "cell": {"column": 2, "row": 2},
        },
    )
    assert flipped.status_code == 200
    assert flipped.get_json()["shortcuts"][0]["mode"] == "focus" and flipped.get_json()["shortcuts"][0]["cell"] == {
        "column": 2,
        "row": 2,
    }
    moved = client.post(
        "/api/desktops/research/shortcuts/move",
        json={"app": "files", "launch": "open", "cell": {"column": 2, "row": 2}},
    )
    assert {entry["target"]["app"]: entry["cell"] for entry in moved.get_json()["shortcuts"]} == {
        "files": {"column": 2, "row": 2},
        "terminal": {"column": 1, "row": 2},
    }
    removed = client.post("/api/desktops/research/shortcuts/remove", json={"app": "files", "launch": "open"})
    assert [entry["target"]["app"] for entry in removed.get_json()["shortcuts"]] == ["terminal"]

    # Deleting a desktop moves the clients on it to the fallback; the last desktop is refused.
    shell.set_client_active_desktop(ClientId("c1"), DesktopId("research"))
    deleted = client.post("/api/desktops/research/delete")
    assert deleted.status_code == 200 and deleted.get_json() == {"fallback_desktop_id": "home"}
    recorded = shell.clients.get_client("c1")
    assert recorded is not None and recorded.active_desktop == "home"
    assert client.post("/api/desktops/research/delete").status_code == 404
    assert client.post("/api/desktops/home/delete").status_code == 409
    types = [message["type"] for message in drain_messages(client_queue)]
    assert "desktops_updated" in types and "active_desktop_changed" in types


def test_windows_open_focus_locate_and_close_across_clients(client: FlaskClient, app: Flask) -> None:
    first_queue = _register_desktop_client(app, "c1", "home")
    second_queue = _register_desktop_client(app, "c2", "home")

    opened = _open_window(client, "terminal", "/new?workdir=%2Ftmp", launch="new")
    assert opened.status_code == 201 and opened.get_json()["is_new"] is True
    window = opened.get_json()["window"]
    assert window["app"] == "terminal" and window["path"] == "/new?workdir=%2Ftmp"
    assert window["title"] == "" and window["is_settling"] is True
    (placed,) = _placements(client, "c1")
    assert placed["window_id"] == window["id"] and placed["is_minimized"] is False and placed["state"] == "NORMAL"
    assert placed["frame"] == {"x": 0.05, "y": 0.06, "width": 0.6, "height": 0.7}
    assert _placements(client, "c2") == []
    # The opener hears of the window before the placement that arranges it.
    announced = [
        message["type"] for message in drain_messages(first_queue) if message["type"] != "active_desktop_changed"
    ]
    assert announced == ["desktops_updated", "placements_updated"]

    # The same app at the same path is answered rather than opened, and raised in the requesting client's layout.
    focused = _open_window(client, "terminal", "/new?workdir=%2Ftmp", client_id="c2")
    assert focused.status_code == 200 and focused.get_json() == {"window": window, "is_new": False}
    (restored,) = _placements(client, "c2")
    assert restored["window_id"] == window["id"] and restored["is_minimized"] is False
    another = _open_window(client, "terminal", "/new?workdir=%2Ftmp", if_present="new")
    assert another.status_code == 201 and another.get_json()["window"]["id"] != window["id"]
    assert [placement["frame"]["x"] for placement in _placements(client, "c1")] == [0.05, 0.08]

    assert _open_window(client, "nope", "/").status_code == 400
    assert _open_window(client, "terminal", "/new", launch="nope").status_code == 400
    assert _open_window(client, "terminal", "//evil").status_code == 400
    assert (
        client.post(
            "/api/desktops/nowhere/windows", json={"app": "terminal", "path": "/", "client_id": "c1"}
        ).status_code
        == 404
    )

    # A location report replaces the path and title, ends the settling, and is silent when it changes nothing.
    located = client.post(
        f"/api/desktops/home/windows/{window['id']}/location",
        json={"path": "/?session=terminal-1", "title": "  Build log  "},
    )
    assert located.status_code == 200
    assert located.get_json()["title"] == "Build log" and located.get_json()["is_settling"] is False
    drain_messages(first_queue)
    again = client.post(
        f"/api/desktops/home/windows/{window['id']}/location",
        json={"path": "/?session=terminal-1", "title": "Build log"},
    )
    assert again.status_code == 200 and drain_messages(first_queue) == []
    assert (
        client.post(
            "/api/desktops/home/windows/win-00000000000000ff/location", json={"path": "/", "title": ""}
        ).status_code
        == 404
    )
    assert (
        client.post(f"/api/desktops/home/windows/{window['id']}/location", json={"path": "x", "title": ""}).status_code
        == 400
    )

    # A close takes the window off the desktop and out of every client's layout, once.
    drain_messages(second_queue)
    assert client.post(f"/api/desktops/home/windows/{window['id']}/close").status_code == 204
    assert [entry["id"] for entry in _desktop_windows(client)] == [another.get_json()["window"]["id"]]
    assert [placement["window_id"] for placement in _placements(client, "c1")] == [another.get_json()["window"]["id"]]
    assert _placements(client, "c2") == []
    messages = drain_messages(second_queue)
    assert "desktops_updated" in [message["type"] for message in messages]
    assert {message["client_id"] for message in messages if message["type"] == "placements_updated"} == {"c1", "c2"}
    assert client.post(f"/api/desktops/home/windows/{window['id']}/close").status_code == 204
    assert drain_messages(second_queue) == []


def test_placements_are_saved_per_client_and_a_stale_save_is_refused(client: FlaskClient, app: Flask) -> None:
    client_queue = _register_desktop_client(app, "c2", "home")
    window = _open_window(client, "terminal", "/?session=terminal-1").get_json()["window"]
    assert client.get("/api/placements/home?client=c2").get_json() == {
        "version": 1,
        "updated_at": None,
        "placements": [],
    }
    assert client.get("/api/placements/home").status_code == 400
    assert client.get("/api/placements/nowhere?client=c2").status_code == 404
    drain_messages(client_queue)

    body = {
        "client_id": "c2",
        "save_id": "save-0000000000000001",
        "placements": [
            {
                "window_id": window["id"],
                "frame": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5},
                "state": "MAXIMIZED",
                "is_minimized": False,
            },
            {
                "window_id": "win-00000000000000ff",
                "frame": {"x": 0, "y": 0, "width": 0.5, "height": 0.5},
                "state": "NORMAL",
                "is_minimized": True,
            },
        ],
    }
    saved = client.post("/api/placements/home", json=body)
    assert saved.status_code == 200 and saved.get_json()["updated_at"] is not None
    stored = client.get("/api/placements/home?client=c2").get_json()
    assert [placement["window_id"] for placement in stored["placements"]] == [window["id"]]
    assert stored["placements"][0]["state"] == "MAXIMIZED" and stored["updated_at"] == saved.get_json()["updated_at"]
    assert [message for message in drain_messages(client_queue) if message["type"] == "placements_updated"] == [
        {"type": "placements_updated", "desktop_id": "home", "client_id": "c2", "save_id": "save-0000000000000001"}
    ]
    unchanged = client.post(
        "/api/placements/home",
        json={**body, "save_id": "save-0000000000000002", "base_updated_at": stored["updated_at"]},
    )
    assert unchanged.status_code == 200 and unchanged.get_json() == {"updated_at": None}
    assert client.post("/api/placements/home", json={**body, "placements": []}).status_code == 409
    fresh = client.post(
        "/api/placements/home",
        json={**body, "save_id": "save-0000000000000003", "base_updated_at": stored["updated_at"], "placements": []},
    )
    assert fresh.status_code == 200 and client.get("/api/placements/home?client=c2").get_json()["placements"] == []
    bad_frame = {
        **body,
        "placements": [{**body["placements"][0], "frame": {"x": 0.8, "y": 0, "width": 0.5, "height": 0.5}}],
    }
    assert (
        client.post(
            "/api/placements/home", json={**bad_frame, "base_updated_at": fresh.get_json()["updated_at"]}
        ).status_code
        == 400
    )


def test_the_inventory_carries_desktops_launch_paths_and_shown_windows(client: FlaskClient, app: Flask) -> None:
    _register_desktop_client(app, "c1", "home")
    _record_client(app, "c2", "everything")
    window = _open_window(client, "terminal", "/?session=terminal-1").get_json()["window"]

    document = client.get("/api/inventory").get_json()
    assert [desktop["id"] for desktop in document["desktops"]] == ["home"]
    assert {entry["name"]: entry["launch_paths"] for entry in document["apps"]} == {
        "terminal": [{"id": "new", "label": "New terminal", "path": "/new", "params": []}],
        "files": [{"id": "open", "label": "Open Files", "path": "/", "params": []}],
    }
    by_id = {entry["id"]: entry for entry in document["clients"]}
    assert by_id["c1"]["active_desktop"] == "home" and by_id["c1"]["shown"] == [window["id"]]
    # A tabbed-shell client with no desktop of its own reads as on the first desktop, with nothing shown.
    assert by_id["c2"]["active_desktop"] == "home" and by_id["c2"]["shown"] == []
    assert by_id["c2"]["docked"] == []
    assert client.get("/api/clients").get_json()["clients"][0]["active_desktop"] == "home"


def _desktop_op(client: FlaskClient, op: str, args: dict[str, Any], requester: dict[str, str] | None) -> Any:
    return client.post("/api/layout/broadcast", json={"op": op, "args": args, "requester": requester})


_TERMINAL_REQUESTER = {"app": "terminal", "marker": "terminal-7"}


def test_desktop_ops_open_and_edit_windows_in_the_target_clients_layout(client: FlaskClient, app: Flask) -> None:
    client_queue = _register_desktop_client(app, "c1", "home")
    requester = _TERMINAL_REQUESTER

    listed = _desktop_op(client, "desktops", {}, requester)
    assert listed.status_code == 200 and [desktop["id"] for desktop in listed.get_json()["desktops"]] == ["home"]

    # An open at a launch path with params, and one at an explicit path.
    opened = _desktop_op(client, "open", {"app": "terminal", "params": {"workdir": "/tmp"}}, requester)
    assert opened.status_code == 200 and opened.get_json()["desktop_id"] == "home"
    first_id = opened.get_json()["window_id"]
    (first,) = opened.get_json()["desktop"]["windows"]
    assert first["path"] == "/new?workdir=%2Ftmp" and first["is_settling"] is True
    assert [placement["window_id"] for placement in opened.get_json()["layout"]["placements"]] == [first_id]
    second_id = _desktop_op(client, "open", {"app": "terminal", "path": "/?session=terminal-7"}, requester).get_json()[
        "window_id"
    ]
    assert [window["is_settling"] for window in _desktop_windows(client)] == [True, False]
    assert (
        _desktop_op(client, "open", {"app": "terminal", "path": "/x", "launch": "new"}, requester).status_code == 400
    )

    # The window verbs, named by id, by app (the most recently focused window of it), and by ``self``.
    focused = _desktop_op(client, "focus", {"window": first_id}, requester)
    assert [placement["window_id"] for placement in focused.get_json()["layout"]["placements"]] == [
        second_id,
        first_id,
    ]
    minimized = _desktop_op(client, "minimize", {"window": "terminal"}, requester)
    assert minimized.get_json()["window_id"] == first_id
    assert minimized.get_json()["layout"]["placements"][-1]["is_minimized"] is True
    placed = _desktop_op(client, "place", {"window": first_id, "zone": "left"}, requester).get_json()
    assert placed["layout"]["placements"][-1]["state"] == "SNAPPED_LEFT"
    assert placed["layout"]["placements"][-1]["is_minimized"] is False
    framed = _desktop_op(client, "place", {"window": first_id, "frame": "0.1,0.2,0.5,0.5"}, requester).get_json()
    assert framed["layout"]["placements"][-1]["state"] == "NORMAL"
    assert framed["layout"]["placements"][-1]["frame"] == {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.5}
    maximized = _desktop_op(client, "maximize", {"window": first_id}, requester).get_json()
    assert maximized["layout"]["placements"][-1]["state"] == "MAXIMIZED"
    restored = _desktop_op(client, "restore", {"window": first_id}, requester).get_json()
    assert restored["layout"]["placements"][-1]["state"] == "NORMAL"
    navigated = _desktop_op(
        client, "navigate", {"window": first_id, "path": "/?session=terminal-9"}, requester
    ).get_json()
    assert [window["path"] for window in navigated["desktop"]["windows"]] == [
        "/?session=terminal-9",
        "/?session=terminal-7",
    ]
    assert _desktop_op(client, "place", {"window": first_id}, requester).status_code == 400
    assert _desktop_op(client, "place", {"window": first_id, "zone": "up"}, requester).status_code == 400
    assert _desktop_op(client, "focus", {"window": "files"}, requester).status_code == 404
    assert _desktop_op(client, "open", {"app": "chat:alice"}, requester).status_code == 400
    assert "app name and a path" in _desktop_op(client, "open", {"app": "chat:alice"}, requester).get_json()["detail"]

    # A refresh of one window goes to the target client; a refresh of a whole app to every window.
    drain_messages(client_queue)
    assert _desktop_op(client, "refresh", {"window": first_id}, requester).get_json()["target_client_id"] == "c1"
    assert _desktop_op(client, "refresh", {"app": "terminal"}, requester).get_json()["target_client_id"] is None
    ops = [message for message in drain_messages(client_queue) if message["type"] == "layout_op"]
    assert [message["args"] for message in ops] == [{"window": first_id}, {"app": "terminal"}]
    assert ops[0]["target_client_id"] == "c1" and ops[1]["target_client_id"] is None

    # ``self`` is the requester's window: the one whose path carries its marker.
    closed = _desktop_op(client, "close", {"window": "self"}, requester)
    assert closed.status_code == 200 and closed.get_json()["window_id"] == second_id
    assert [window["id"] for window in _desktop_windows(client)] == [first_id]
    assert _desktop_op(client, "close", {"window": "self"}, requester).status_code == 404
    assert _desktop_op(client, "close", {"window": "self"}, None).status_code == 400

    # The address verbs still dispatch on the same route: a close of an address edits the dockview arrangement
    # (a client of the desktop shell has no view of its own, so one must be named).
    assert _broadcast(client, "close", {"address": str(_TERMINAL_1)}).status_code == 412
    assert _broadcast(client, "close", {"address": str(_TERMINAL_1), "view": "everything"}).status_code == 404


def test_a_desktop_op_targets_the_named_desktop_and_load_switches_the_client(client: FlaskClient, app: Flask) -> None:
    """``--desktop`` edits that desktop and switches the client to it; ``load`` switches alone."""
    shell = _shell(app)
    client_queue = _register_desktop_client(app, "c1", "home")
    requester = _TERMINAL_REQUESTER
    client.post("/api/desktops", json={"name": "Research", "color": "#12B5A5", "glyph": 4})
    drain_messages(client_queue)

    on_research = _desktop_op(client, "open", {"app": "files", "desktop": "Research"}, requester)
    assert on_research.status_code == 200 and on_research.get_json()["desktop_id"] == "research"
    recorded = shell.clients.get_client("c1")
    assert recorded is not None and recorded.active_desktop == "research"
    assert "active_desktop_changed" in [message["type"] for message in drain_messages(client_queue)]
    loaded = _desktop_op(client, "load", {"desktop": "home"}, requester)
    assert loaded.status_code == 200 and loaded.get_json()["desktop_id"] == "home"
    assert _desktop_op(client, "load", {"desktop": "Nowhere"}, requester).status_code == 404
    assert _desktop_op(client, "load", {}, requester).status_code == 400


def test_desktop_shortcut_and_wallpaper_ops_edit_the_target_desktop(client: FlaskClient, app: Flask) -> None:
    _register_desktop_client(app, "c1", "home")
    requester = _TERMINAL_REQUESTER

    shortcuts = _desktop_op(client, "shortcuts", {}, requester).get_json()["desktop"]["shortcuts"]
    assert [entry["target"]["app"] for entry in shortcuts] == ["terminal", "files"]
    added = _desktop_op(
        client, "shortcut_set", {"app": "files", "launch": "open", "mode": "new"}, requester
    ).get_json()
    assert added["desktop"]["shortcuts"][1]["mode"] == "new"
    moved = _desktop_op(
        client, "shortcut_move", {"app": "files", "launch": "open", "cell": "3,1"}, requester
    ).get_json()
    assert moved["desktop"]["shortcuts"][1]["cell"] == {"column": 3, "row": 1}
    assert _desktop_op(client, "shortcut_move", {"app": "files", "launch": "open"}, requester).status_code == 400
    removed = _desktop_op(client, "shortcut_remove", {"app": "files", "launch": "open"}, requester).get_json()
    assert [entry["target"]["app"] for entry in removed["desktop"]["shortcuts"]] == ["terminal"]
    assert (
        _desktop_op(client, "wallpaper", {"wallpaper": {"kind": "bundled", "name": "nope"}}, requester).status_code
        == 404
    )
    assert _desktop_op(client, "wallpaper", {"wallpaper": None}, requester).get_json()["desktop"]["wallpaper"] is None


def test_inspect_answers_no_view_when_only_desktop_clients_are_known(client: FlaskClient, app: Flask) -> None:
    """A desktop-shell client is on no view, so the address verbs' ``inspect`` settles on none rather than on the
    empty view it registered or the ``None`` its record holds: with two of them connected, and with only their
    records left."""
    queues = [_register_desktop_client(app, "c1", "home"), _register_desktop_client(app, "c2", "home")]
    connected = _broadcast(client, "inspect").get_json()
    assert connected["view_id"] is None and connected["client_id"] is None
    for client_queue in queues:
        _shell(app).broadcaster.unregister(client_queue)
    recorded = _broadcast(client, "inspect").get_json()
    assert recorded["view_id"] is None and recorded["client_id"] is None


def test_a_desktop_op_with_no_client_to_target_is_a_412(client: FlaskClient) -> None:
    refused = _desktop_op(client, "open", {"app": "terminal"}, None)
    assert refused.status_code == 412 and "--client" in refused.get_json()["detail"]
    assert _desktop_op(client, "open", {"app": "terminal", "client": "ghost"}, None).status_code == 404
    # An argument the op itself needs is reported before any client is looked for.
    assert _desktop_op(client, "load", {}, None).status_code == 400


def test_a_whole_app_refresh_reaches_every_client_and_needs_no_target(client: FlaskClient, app: Flask) -> None:
    first_queue = _register_desktop_client(app, "c1", "home")
    second_queue = _register_desktop_client(app, "c2", "home")
    window_id = _open_window(client, "terminal", "/?session=terminal-1").get_json()["window"]["id"]
    drain_messages(first_queue)
    drain_messages(second_queue)

    # With two clients connected and none named, a whole-app refresh still goes out to both, while a
    # one-window refresh cannot tell which client's page it means.
    everywhere = _desktop_op(client, "refresh", {"app": "terminal"}, None)
    assert everywhere.status_code == 200 and everywhere.get_json()["target_client_id"] is None
    assert _desktop_op(client, "refresh", {"window": window_id}, None).status_code == 412
    for client_queue in (first_queue, second_queue):
        ops = [message for message in drain_messages(client_queue) if message["type"] == "layout_op"]
        assert [(message["args"], message["target_client_id"]) for message in ops] == [({"app": "terminal"}, None)]


@pytest.mark.parametrize(
    ("op", "args", "fragment"),
    [
        ("shortcut_set", {"app": "files", "launch": "open", "mode": "bogus"}, "mode"),
        ("shortcut_set", {"app": "files", "launch": "", "mode": "new"}, "launch"),
        ("shortcut_set", {"app": "files", "mode": "new"}, "launch"),
        ("shortcut_remove", {"app": "files", "launch": "Not Valid"}, "launch"),
        ("open", {"app": "terminal", "launch": "Not Valid"}, "launch"),
        ("wallpaper", {"wallpaper": {"kind": "nope", "name": "x"}}, "wallpaper"),
        ("wallpaper", {"wallpaper": {"kind": "bundled"}}, "wallpaper"),
    ],
)
def test_a_malformed_desktop_op_argument_is_a_400_naming_the_argument(
    client: FlaskClient, app: Flask, op: str, args: dict[str, Any], fragment: str
) -> None:
    _register_desktop_client(app, "c1", "home")
    refused = _desktop_op(client, op, args, _TERMINAL_REQUESTER)
    assert refused.status_code == 400
    assert fragment in refused.get_json()["detail"]
