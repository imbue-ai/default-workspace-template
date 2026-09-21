"""Tests for the shell's HTTP routes (desktop contracts.md sections 5, 6, and 8) over a test state and the two-app registry."""

import queue
from pathlib import Path
from typing import Any

import pytest
from app_manifest.primitives import AppName
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.layout_ops import OpRequester
from imbue.system_interface.shell.liveness import probe_all_app_liveness
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.route_helpers import resolve_client
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.testing import TEST_NOW
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import drain_messages
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.shell.wallpapers import BUNDLED_WALLPAPERS_DIRNAME
from imbue.system_interface.testing import FakeSupervisorServer
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_NOT_LOOPBACK = {"REMOTE_ADDR": "10.0.0.7"}
_TERMINAL_REQUESTER = {"app": "terminal", "marker": "terminal-7"}


def _shell(app: Flask) -> ShellState:
    return state_of(app).shell


def _register_client(app: Flask, client_id: str, desktop_id: str = "home") -> "queue.Queue[str | None]":
    """A connected window of ``client_id`` on ``desktop_id``, recorded the way its ``client_state`` report records it."""
    client_queue = _shell(app).broadcaster.register()
    _shell(app).broadcaster.set_client_info(client_queue, client_id, desktop_id)
    _shell(app).record_client_report(
        ClientStateReport(client_id=ClientId(client_id), active_desktop=DesktopId(desktop_id))
    )
    return client_queue


def _record_client(app: Flask, client_id: str, desktop_id: str = "home") -> None:
    """A client the shell has a record of but that is not connected."""
    _shell(app).clients.record_report(
        ClientStateReport(client_id=ClientId(client_id), active_desktop=DesktopId(desktop_id)), TEST_NOW
    )


def _open_window(client: FlaskClient, app_name: str, path: str, client_id: str = "c1", **extra: Any) -> Any:
    return client.post(
        "/api/desktops/home/windows", json={"app": app_name, "path": path, "client_id": client_id, **extra}
    )


def _placements(client: FlaskClient, client_id: str) -> list[dict[str, Any]]:
    return client.get(f"/api/placements/home?client={client_id}").get_json()["placements"]


def _desktop_windows(client: FlaskClient) -> list[dict[str, Any]]:
    (home,) = client.get("/api/desktops").get_json()["desktops"]
    return home["windows"]


def _op(client: FlaskClient, op: str, args: dict[str, Any], requester: dict[str, str] | None) -> Any:
    """Post an op the way ``layout.py`` does."""
    return client.post("/api/layout/broadcast", json={"op": op, "args": args, "requester": requester})


# Section 6: the client-activity report


def test_client_activity_is_appended_with_its_desktop(client: FlaskClient, app: Flask) -> None:
    message = {
        "client_id": "c1",
        "desktop_id": "home",
        "kind": "message",
        "app": "chat",
        "key": "agent-1",
        "text": "hi",
    }
    assert client.post("/api/client-activity", json=message).status_code == 204
    assert client.post("/api/client-activity", json={**message, "kind": "view_switch"}).status_code == 400
    # A report is what the shell's pages send; a view and a device kind are refused.
    assert (
        client.post(
            "/api/client-activity",
            json={
                "client_id": "c1",
                "device_kind": "desktop",
                "view_id": "everything",
                "kind": "message",
                "app": "chat",
                "key": "agent-1",
            },
        ).status_code
        == 400
    )
    assert client.post("/api/client-activity", json=message, environ_base=_NOT_LOOPBACK).status_code == 403
    events = _shell(app).activity.read_events()
    assert [(event["type"], event["client_id"], event["desktop_id"]) for event in events] == [
        ("message", "c1", "home")
    ]
    assert events[0]["key"] == "agent-1" and events[0]["text"] == "hi"


# Section 5: stop and start


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
        registry_row_toml("chat", "http://localhost:8000", program="system_interface"),
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


# Section 5.5: clients and the inventory


def test_clients_and_the_inventory_document_are_served(client: FlaskClient, app: Flask) -> None:
    _register_client(app, "c1", "home")
    _record_client(app, "c2", "home")
    window = _open_window(client, "terminal", "/?session=terminal-1").get_json()["window"]

    clients = client.get("/api/clients").get_json()["clients"]
    assert [(entry["id"], entry["is_connected"], entry["active_desktop"], entry["entries"]) for entry in clients] == [
        ("c1", True, "home", {}),
        ("c2", False, "home", {}),
    ]

    document = client.get("/api/inventory").get_json()
    assert set(document) == {"desktops", "apps", "clients"}
    assert [desktop["id"] for desktop in document["desktops"]] == ["home"]
    assert [window["id"] for window in document["desktops"][0]["windows"]] == [window["id"]]
    assert {entry["name"]: entry["launch_paths"] for entry in document["apps"]} == {
        "terminal": [{"id": "new", "label": "New terminal", "path": "/new", "params": []}],
        "files": [{"id": "open", "label": "Open Files", "path": "/", "params": []}],
    }
    by_id = {entry["id"]: entry for entry in document["clients"]}
    assert by_id["c1"]["active_desktop"] == "home" and by_id["c1"]["shown"] == [window["id"]]
    assert by_id["c1"]["is_connected"] is True
    # A recorded client with no layout of its own has nothing shown.
    assert by_id["c2"]["active_desktop"] == "home" and by_id["c2"]["shown"] == []
    assert by_id["c2"]["is_connected"] is False


def test_a_client_stored_on_a_desktop_that_does_not_exist_is_listed_on_the_first_one(
    client: FlaskClient, app: Flask
) -> None:
    """A record naming a desktop nothing holds (a deleted one, or a view a version-1 clients file recorded) reads
    as the first desktop from both client listings, so neither names a desktop a reader cannot find."""
    _record_client(app, "c-old", "everything")

    (listed,) = client.get("/api/clients").get_json()["clients"]
    (in_inventory,) = client.get("/api/inventory").get_json()["clients"]

    assert (listed["id"], listed["active_desktop"]) == ("c-old", "home")
    assert (in_inventory["id"], in_inventory["active_desktop"]) == ("c-old", "home")


# Section 8: the op route


def test_the_op_route_validates_its_input(client: FlaskClient) -> None:
    assert _op(client, "context", {}, _TERMINAL_REQUESTER).status_code == 200
    assert client.post("/api/layout/broadcast", json={"op": "context"}, environ_base=_NOT_LOOPBACK).status_code == 403
    assert _op(client, "explode", {}, _TERMINAL_REQUESTER).status_code == 400
    for retired in ("inspect", "views", "split", "move", "rename", "delete", "replace-url"):
        assert _op(client, retired, {}, _TERMINAL_REQUESTER).status_code == 400, retired
    assert client.post("/api/layout/broadcast", json={"op": "open", "args": []}).status_code == 400
    assert client.post("/api/layout/broadcast", data="{", content_type="application/json").status_code == 400
    # A requester that is not ``{app, marker}`` is refused, not dropped: dropped, the op would lose its
    # attribution and ``self`` would be reported as unset although the caller sent one.
    for malformed in ("app:chat?instance=agent-1", "chat:agent-1", 7, 0, False, [], {"app": "Bad Name"}):
        refused = client.post("/api/layout/broadcast", json={"op": "context", "requester": malformed})
        assert refused.status_code == 400, malformed
        assert "requester" in refused.get_json()["detail"]


def test_context_summarizes_every_client_from_the_log_and_the_live_registrations(
    client: FlaskClient, app: Flask
) -> None:
    shell = _shell(app)
    _register_client(app, "c1", "home")
    shell.activity.append_message("c1", "home", "chat", "agent-1", "hello")
    # A second client that has connected and done nothing else: it has no event in the log.
    _register_client(app, "c9", "home")

    context = _op(client, "context", {}, _TERMINAL_REQUESTER).get_json()["clients"]
    assert [entry["client_id"] for entry in context] == ["c1", "c9"]
    assert context[0]["is_connected"] is True and context[0]["active_desktop"] == "home"
    assert (context[0]["recent_messages"][0]["app"], context[0]["recent_messages"][0]["key"]) == ("chat", "agent-1")
    assert context[1] == {
        "client_id": "c9",
        "active_desktop": "home",
        "last_seen": "",
        "is_connected": True,
        "recent_messages": [],
    }


def test_an_op_is_attributed_to_the_client_that_last_messaged_the_requesting_agent(
    client: FlaskClient, app: Flask
) -> None:
    """With several clients connected, the requester's own client is the one that last messaged its chat; an
    explicit ``client`` outranks that, and with neither there is no client to apply the op to."""
    shell = _shell(app)
    _register_client(app, "c1", "home")
    _register_client(app, "c7", "home")
    requester = {"app": "chat", "marker": "agent-1"}

    assert _op(client, "open", {"app": "files"}, requester).status_code == 412

    shell.activity.append_message("c7", "home", "chat", "agent-1", "hello")
    attributed = _op(client, "open", {"app": "files"}, requester)
    assert attributed.status_code == 200 and attributed.get_json()["client_id"] == "c7"
    assert _op(client, "open", {"app": "files"}, {"app": "chat", "marker": "agent-2"}).status_code == 412
    explicit = _op(client, "open", {"app": "files", "client": "c1"}, requester)
    assert explicit.status_code == 200 and explicit.get_json()["client_id"] == "c1"
    # A client id names a layout file, so one outside the id's alphabet is refused before any read.
    assert _op(client, "open", {"app": "files", "client": "../c1"}, requester).status_code == 400
    assert _op(client, "open", {"app": "files", "client": "ghost"}, requester).status_code == 404


def test_a_bare_app_requester_is_attributed_to_no_client(app: Flask) -> None:
    """A requester that names an app and no marker has no client that last messaged it: the log is not
    searched under a made-up key."""
    shell = _shell(app)
    _register_client(app, "c7", "home")
    shell.activity.append_message("c7", "home", "files", "None", "hello")
    _register_client(app, "c1", "home")

    assert resolve_client(shell, {}, OpRequester(app=AppName("files"), marker="")) is None


# Pinned windows (pinned-taskbar-entries plan sections 3.2 and 4.5)


def _pinned_shell(
    tmp_path: Path,
    broadcaster: WebSocketBroadcaster,
    *extra_rows: str,
    pin: tuple[str, str, str, str] = ("/", "plain", "linked", "bar"),
) -> Flask:
    """The shell over the two-app registry plus a ``buddy`` app pinned as ``pin`` (path, style, scope, default
    mode) and ``extra_rows``."""
    registry_path = write_two_app_registry(
        tmp_path, registry_row_toml("buddy", "http://localhost:7002", pin=pin), *extra_rows
    )
    return shell_application(tmp_path, build_inventory(registry_path, broadcaster), broadcaster)


def test_every_desktop_holds_one_pinned_window_per_pinned_app_and_a_new_desktop_is_born_with_one(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    app = _pinned_shell(tmp_path, broadcaster)
    client = app.test_client()
    # A connected window that has read nothing yet: the route's read below is the first, and reconciles.
    client_queue = _shell(app).broadcaster.register()
    _shell(app).broadcaster.set_client_info(client_queue, "c1", "home")

    (home,) = client.get("/api/desktops").get_json()["desktops"]
    (pinned,) = home["windows"]
    assert (pinned["app"], pinned["path"], pinned["is_pinned"], pinned["scope"]) == ("buddy", "/", True, "linked")
    assert pinned["title"] == "" and pinned["is_settling"] is False
    # The reconcile that created it was announced once, after the read; a second read announces nothing.
    assert [message["type"] for message in drain_messages(client_queue)] == ["desktops_updated"]
    assert client.get("/api/desktops").get_json()["desktops"][0]["windows"] == [pinned]
    assert drain_messages(client_queue) == []
    # The inventory's app carries the pin.
    by_name = {entry["name"]: entry for entry in client.get("/api/inventory").get_json()["apps"]}
    assert by_name["buddy"]["pin"] == {"path": "/", "style": "plain", "scope": "linked", "default_mode": "bar"}
    assert by_name["files"]["pin"] is None

    created = client.post("/api/desktops", json={"name": "Research", "color": "#12B5A5", "glyph": 4}).get_json()
    (born,) = created["windows"]
    assert born["app"] == "buddy" and born["is_pinned"] is True and born["id"] != pinned["id"]
    # An open at the home path with the default focus behaviour finds the pinned window.
    focused = _open_window(client, "buddy", "/")
    assert focused.status_code == 200 and focused.get_json() == {"window": pinned, "is_new": False}


def test_a_pinned_window_is_refused_a_close_by_the_route_and_by_the_op(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    app = _pinned_shell(tmp_path, broadcaster)
    client = app.test_client()
    _register_client(app, "c1", "home")
    (pinned,) = client.get("/api/desktops").get_json()["desktops"][0]["windows"]

    refused = client.post(f"/api/desktops/home/windows/{pinned['id']}/close")
    assert refused.status_code == 409 and "minimize" in refused.get_json()["detail"]
    op_refused = _op(client, "close", {"window": pinned["id"]}, _TERMINAL_REQUESTER)
    assert op_refused.status_code == 400 and "minimize" in op_refused.get_json()["detail"]
    assert [window["id"] for window in _desktop_windows(client)] == [pinned["id"]]
    # Minimizing is what the op offers instead, and an ordinary window still closes.
    minimized = _op(client, "minimize", {"window": pinned["id"]}, _TERMINAL_REQUESTER)
    assert minimized.get_json()["layout"]["placements"][-1]["is_minimized"] is True
    ordinary = _open_window(client, "buddy", "/?doc=2", if_present="new").get_json()["window"]
    assert client.post(f"/api/desktops/home/windows/{ordinary['id']}/close").status_code == 204


def test_an_app_registered_later_is_reconciled_on_the_next_read_and_a_withdrawn_pin_frees_its_window(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry_path = write_two_app_registry(tmp_path)
    inventory = build_inventory(registry_path, broadcaster)
    client = shell_application(tmp_path, inventory, broadcaster).test_client()
    assert client.get("/api/desktops").get_json()["desktops"][0]["windows"] == []

    write_two_app_registry(
        tmp_path, registry_row_toml("buddy", "http://localhost:7002", pin=("/", "plain", "linked", "bar"))
    )
    inventory.reload_registry()
    (pinned,) = client.get("/api/desktops").get_json()["desktops"][0]["windows"]
    assert pinned["app"] == "buddy" and pinned["is_pinned"] is True

    write_two_app_registry(tmp_path, registry_row_toml("buddy", "http://localhost:7002"))
    inventory.reload_registry()
    (freed,) = client.get("/api/desktops").get_json()["desktops"][0]["windows"]
    assert freed["id"] == pinned["id"] and freed["is_pinned"] is False
    assert client.post(f"/api/desktops/home/windows/{freed['id']}/close").status_code == 204


def test_an_independent_window_keeps_a_path_per_client_and_its_shared_path_stays_the_home_path(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """A report on an independent window is stored for the reporting client alone, announced to that client with
    a shell-minted save id, and answered as that client sees the window; the shared record keeps the home path
    and an empty title, another client sees its own path (or the home path), and ``navigate`` moves one client."""
    app = _pinned_shell(tmp_path, broadcaster, pin=("/", "plain", "independent", "bar"))
    client = app.test_client()
    first_queue = _register_client(app, "c1", "home")
    second_queue = _register_client(app, "c2", "home")
    (pinned,) = client.get("/api/desktops").get_json()["desktops"][0]["windows"]
    assert pinned["scope"] == "independent"
    drain_messages(first_queue)
    drain_messages(second_queue)

    located = client.post(
        f"/api/desktops/home/windows/{pinned['id']}/location",
        json={"client_id": "c1", "path": "/?doc=1", "title": "First"},
    )
    assert located.status_code == 200
    assert (located.get_json()["path"], located.get_json()["title"]) == ("/?doc=1", "First")
    shared = client.get("/api/desktops").get_json()["desktops"][0]["windows"][0]
    assert (shared["path"], shared["title"]) == ("/", "")
    # The reporting client's windows are told through placements_updated naming that client (which, as with every
    # layout write, every window hears and only that client's apply); the shared desktops are not re-announced.
    announced = drain_messages(first_queue)
    assert [(message["type"], message["client_id"]) for message in announced] == [("placements_updated", "c1")]
    assert announced[0]["save_id"].startswith("save-")
    assert [(message["type"], message["client_id"]) for message in drain_messages(second_queue)] == [
        ("placements_updated", "c1")
    ]
    layout = client.get("/api/placements/home?client=c1").get_json()
    assert layout["window_paths"] == {pinned["id"]: {"path": "/?doc=1", "title": "First"}}
    assert layout["updated_at"] is None
    assert client.get("/api/placements/home?client=c2").get_json()["window_paths"] == {}
    # The same report again is silent.
    client.post(
        f"/api/desktops/home/windows/{pinned['id']}/location",
        json={"client_id": "c1", "path": "/?doc=1", "title": "First"},
    )
    assert drain_messages(first_queue) == []
    # A report without a client is refused.
    assert (
        client.post(
            f"/api/desktops/home/windows/{pinned['id']}/location", json={"path": "/x", "title": ""}
        ).status_code
        == 400
    )

    # ``navigate`` writes the target client's path and keeps its title; the other client is untouched.
    navigated = _op(client, "navigate", {"window": pinned["id"], "path": "/?doc=2", "client": "c1"}, None)
    assert navigated.status_code == 200
    assert navigated.get_json()["layout"]["window_paths"] == {pinned["id"]: {"path": "/?doc=2", "title": "First"}}
    assert navigated.get_json()["desktop"]["windows"][0]["path"] == "/"
    assert [message["type"] for message in drain_messages(first_queue)] == ["placements_updated"]
    assert client.get("/api/placements/home?client=c2").get_json()["window_paths"] == {}
    drain_messages(second_queue)
    # A linked window still moves for everyone through the same route.
    linked = _open_window(client, "terminal", "/?session=t1").get_json()["window"]
    client.post(
        f"/api/desktops/home/windows/{linked['id']}/location",
        json={"client_id": "c2", "path": "/?session=t2", "title": "Two"},
    )
    assert [window["path"] for window in _desktop_windows(client)] == ["/", "/?session=t2"]
    assert "desktops_updated" in [message["type"] for message in drain_messages(first_queue)]


def test_a_clients_stored_paths_on_other_desktops_survive_a_report(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    """A client's window-paths file spans every desktop: a report on one desktop's independent pinned window keeps
    the same client's stored path for another desktop's, and each layout answers its own desktop's entry."""
    app = _pinned_shell(tmp_path, broadcaster, pin=("/", "plain", "independent", "bar"))
    client = app.test_client()
    _register_client(app, "c1", "home")
    (home_pinned,) = client.get("/api/desktops").get_json()["desktops"][0]["windows"]
    created = client.post("/api/desktops", json={"name": "Research", "color": "#12B5A5", "glyph": 4}).get_json()
    (research_pinned,) = created["windows"]

    for desktop_id, window_id, doc in (("home", home_pinned["id"], 1), ("research", research_pinned["id"], 2)):
        client.post(
            f"/api/desktops/{desktop_id}/windows/{window_id}/location",
            json={"client_id": "c1", "path": f"/?doc={doc}", "title": f"Doc {doc}"},
        )
    assert client.get("/api/placements/home?client=c1").get_json()["window_paths"] == {
        home_pinned["id"]: {"path": "/?doc=1", "title": "Doc 1"}
    }
    assert client.get("/api/placements/research?client=c1").get_json()["window_paths"] == {
        research_pinned["id"]: {"path": "/?doc=2", "title": "Doc 2"}
    }


def test_a_clients_entry_presentation_is_written_announced_to_that_client_and_checked_against_the_pin(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    app = _pinned_shell(tmp_path, broadcaster, pin=("/", "avatar", "linked", "floating"))
    client = app.test_client()
    first_queue = _register_client(app, "c1", "home")
    second_queue = _register_client(app, "c2", "home")
    drain_messages(first_queue)
    drain_messages(second_queue)

    written = client.post(
        "/api/clients/c1/entries/buddy",
        json={"mode": "floating", "style": "avatar", "position": {"x": 0.25, "y": 0.5}},
    )
    assert written.status_code == 200
    assert written.get_json()["entries"] == {
        "buddy": {"mode": "floating", "style": "avatar", "position": {"x": 0.25, "y": 0.5}}
    }
    assert written.get_json()["is_connected"] is True
    assert drain_messages(first_queue) == [
        {
            "type": "client_entries_changed",
            "client_id": "c1",
            "entries": {"buddy": {"mode": "floating", "style": "avatar", "position": {"x": 0.25, "y": 0.5}}},
        }
    ]
    assert drain_messages(second_queue) == []
    listed = {entry["id"]: entry for entry in client.get("/api/clients").get_json()["clients"]}
    assert "buddy" in listed["c1"]["entries"] and listed["c2"]["entries"] == {}
    # The plain style is always on offer; a style the pin does not declare, an unpinned app, an unknown app, a
    # position outside the backdrop, and an unknown client are refused.
    plain = client.post("/api/clients/c1/entries/buddy", json={"mode": "bar", "style": "plain", "position": None})
    assert plain.status_code == 200 and plain.get_json()["entries"]["buddy"]["mode"] == "bar"
    assert (
        client.post(
            "/api/clients/c1/entries/buddy", json={"mode": "bar", "style": "dot", "position": None}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/clients/c1/entries/files", json={"mode": "bar", "style": "plain", "position": None}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/clients/c1/entries/nope", json={"mode": "bar", "style": "plain", "position": None}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/clients/c1/entries/buddy",
            json={"mode": "floating", "style": "plain", "position": {"x": 1.5, "y": 0}},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/clients/ghost/entries/buddy", json={"mode": "bar", "style": "plain", "position": None}
        ).status_code
        == 404
    )


# The desktop routes (desktop contracts.md section 5)


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
    client_queue = _register_client(app, "c1", "home")
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
    first_queue = _register_client(app, "c1", "home")
    second_queue = _register_client(app, "c2", "home")

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
        json={"client_id": "c1", "path": "/?session=terminal-1", "title": "  Build log  "},
    )
    assert located.status_code == 200
    assert located.get_json()["title"] == "Build log" and located.get_json()["is_settling"] is False
    drain_messages(first_queue)
    again = client.post(
        f"/api/desktops/home/windows/{window['id']}/location",
        json={"client_id": "c1", "path": "/?session=terminal-1", "title": "Build log"},
    )
    assert again.status_code == 200 and drain_messages(first_queue) == []
    assert (
        client.post(
            "/api/desktops/home/windows/win-00000000000000ff/location",
            json={"client_id": "c1", "path": "/", "title": ""},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/desktops/home/windows/{window['id']}/location", json={"client_id": "c1", "path": "x", "title": ""}
        ).status_code
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
    client_queue = _register_client(app, "c2", "home")
    window = _open_window(client, "terminal", "/?session=terminal-1").get_json()["window"]
    assert client.get("/api/placements/home?client=c2").get_json() == {
        "version": 1,
        "updated_at": None,
        "placements": [],
        "window_paths": {},
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


# Section 8: the verbs of the op route


def test_ops_open_and_edit_windows_in_the_target_clients_layout(client: FlaskClient, app: Flask) -> None:
    client_queue = _register_client(app, "c1", "home")
    requester = _TERMINAL_REQUESTER

    listed = _op(client, "desktops", {}, requester)
    assert listed.status_code == 200 and [desktop["id"] for desktop in listed.get_json()["desktops"]] == ["home"]
    # The read ops answer the whole inventory document, apps and clients included.
    assert set(listed.get_json()) == {"ok", "desktops", "apps", "clients"}
    assert [entry["id"] for entry in listed.get_json()["clients"]] == ["c1"]

    # An open at a launch path with params, and one at an explicit path.
    opened = _op(client, "open", {"app": "terminal", "params": {"workdir": "/tmp"}}, requester)
    assert opened.status_code == 200 and opened.get_json()["desktop_id"] == "home"
    first_id = opened.get_json()["window_id"]
    (first,) = opened.get_json()["desktop"]["windows"]
    assert first["path"] == "/new?workdir=%2Ftmp" and first["is_settling"] is True
    assert [placement["window_id"] for placement in opened.get_json()["layout"]["placements"]] == [first_id]
    second_id = _op(client, "open", {"app": "terminal", "path": "/?session=terminal-7"}, requester).get_json()[
        "window_id"
    ]
    assert [window["is_settling"] for window in _desktop_windows(client)] == [True, False]
    assert _op(client, "open", {"app": "terminal", "path": "/x", "launch": "new"}, requester).status_code == 400

    # The window verbs, named by id, by app (the most recently focused window of it), and by ``self``.
    focused = _op(client, "focus", {"window": first_id}, requester)
    assert [placement["window_id"] for placement in focused.get_json()["layout"]["placements"]] == [
        second_id,
        first_id,
    ]
    minimized = _op(client, "minimize", {"window": "terminal"}, requester)
    assert minimized.get_json()["window_id"] == first_id
    assert minimized.get_json()["layout"]["placements"][-1]["is_minimized"] is True
    placed = _op(client, "place", {"window": first_id, "zone": "left"}, requester).get_json()
    assert placed["layout"]["placements"][-1]["state"] == "SNAPPED_LEFT"
    assert placed["layout"]["placements"][-1]["is_minimized"] is False
    framed = _op(client, "place", {"window": first_id, "frame": "0.1,0.2,0.5,0.5"}, requester).get_json()
    assert framed["layout"]["placements"][-1]["state"] == "NORMAL"
    assert framed["layout"]["placements"][-1]["frame"] == {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.5}
    maximized = _op(client, "maximize", {"window": first_id}, requester).get_json()
    assert maximized["layout"]["placements"][-1]["state"] == "MAXIMIZED"
    restored = _op(client, "restore", {"window": first_id}, requester).get_json()
    assert restored["layout"]["placements"][-1]["state"] == "NORMAL"
    navigated = _op(client, "navigate", {"window": first_id, "path": "/?session=terminal-9"}, requester).get_json()
    assert [window["path"] for window in navigated["desktop"]["windows"]] == [
        "/?session=terminal-9",
        "/?session=terminal-7",
    ]
    assert _op(client, "place", {"window": first_id}, requester).status_code == 400
    assert _op(client, "place", {"window": first_id, "zone": "up"}, requester).status_code == 400
    assert _op(client, "focus", {"window": "files"}, requester).status_code == 404
    refused = _op(client, "open", {"app": "chat:alice"}, requester)
    assert refused.status_code == 400 and "app name and a path" in refused.get_json()["detail"]

    # A refresh of one window goes to the target client; a refresh of a whole app and the interface reload to
    # every window.
    drain_messages(client_queue)
    assert _op(client, "refresh", {"window": first_id}, requester).get_json()["target_client_id"] == "c1"
    assert _op(client, "refresh", {"app": "terminal"}, requester).get_json()["target_client_id"] is None
    assert _op(client, "reload_system_interface", {}, None).get_json()["target_client_id"] is None
    ops = [message for message in drain_messages(client_queue) if message["type"] == "layout_op"]
    assert [(message["op"], message["args"]) for message in ops] == [
        ("refresh", {"window": first_id}),
        ("refresh", {"app": "terminal"}),
        ("reload_system_interface", {}),
    ]
    assert [message["target_client_id"] for message in ops] == ["c1", None, None]
    assert ops[0]["requester"] == "terminal:terminal-7" and ops[2]["requester"] == ""

    # ``self`` is the requester's window: the one whose path carries its marker.
    closed = _op(client, "close", {"window": "self"}, requester)
    assert closed.status_code == 200 and closed.get_json()["window_id"] == second_id
    assert [window["id"] for window in _desktop_windows(client)] == [first_id]
    assert _op(client, "close", {"window": "self"}, requester).status_code == 404
    assert _op(client, "close", {"window": "self"}, None).status_code == 400


def test_an_op_lands_with_no_browser_connected_on_a_recorded_client(client: FlaskClient, app: Flask) -> None:
    """A client the shell has a record of is a target once named, so an agent arranges a desktop for a browser
    that is not connected; the write is announced for whenever it connects."""
    _record_client(app, "c1", "home")
    requester = _TERMINAL_REQUESTER

    assert _op(client, "open", {"app": "files"}, requester).status_code == 412
    opened = _op(client, "open", {"app": "files", "client": "c1"}, requester)
    assert opened.status_code == 200
    assert [placement["window_id"] for placement in _placements(client, "c1")] == [opened.get_json()["window_id"]]


def test_an_op_targets_the_named_desktop_and_load_switches_the_client(client: FlaskClient, app: Flask) -> None:
    """``--desktop`` edits that desktop and switches the client to it; ``load`` switches alone."""
    shell = _shell(app)
    client_queue = _register_client(app, "c1", "home")
    requester = _TERMINAL_REQUESTER
    client.post("/api/desktops", json={"name": "Research", "color": "#12B5A5", "glyph": 4})
    drain_messages(client_queue)

    on_research = _op(client, "open", {"app": "files", "desktop": "Research"}, requester)
    assert on_research.status_code == 200 and on_research.get_json()["desktop_id"] == "research"
    recorded = shell.clients.get_client("c1")
    assert recorded is not None and recorded.active_desktop == "research"
    assert "active_desktop_changed" in [message["type"] for message in drain_messages(client_queue)]
    loaded = _op(client, "load", {"desktop": "home"}, requester)
    assert loaded.status_code == 200 and loaded.get_json()["desktop_id"] == "home"
    assert _op(client, "load", {"desktop": "Nowhere"}, requester).status_code == 404
    assert _op(client, "load", {}, requester).status_code == 400


def test_shortcut_and_wallpaper_ops_edit_the_target_desktop(client: FlaskClient, app: Flask) -> None:
    _register_client(app, "c1", "home")
    requester = _TERMINAL_REQUESTER

    shortcuts = _op(client, "shortcuts", {}, requester).get_json()["desktop"]["shortcuts"]
    assert [entry["target"]["app"] for entry in shortcuts] == ["terminal", "files"]
    added = _op(client, "shortcut_set", {"app": "files", "launch": "open", "mode": "new"}, requester).get_json()
    assert added["desktop"]["shortcuts"][1]["mode"] == "new"
    moved = _op(client, "shortcut_move", {"app": "files", "launch": "open", "cell": "3,1"}, requester).get_json()
    assert moved["desktop"]["shortcuts"][1]["cell"] == {"column": 3, "row": 1}
    assert _op(client, "shortcut_move", {"app": "files", "launch": "open"}, requester).status_code == 400
    removed = _op(client, "shortcut_remove", {"app": "files", "launch": "open"}, requester).get_json()
    assert [entry["target"]["app"] for entry in removed["desktop"]["shortcuts"]] == ["terminal"]
    assert _op(client, "wallpaper", {"wallpaper": {"kind": "bundled", "name": "nope"}}, requester).status_code == 404
    assert _op(client, "wallpaper", {"wallpaper": None}, requester).get_json()["desktop"]["wallpaper"] is None


def test_an_op_with_no_client_to_target_is_a_412(client: FlaskClient) -> None:
    refused = _op(client, "open", {"app": "terminal"}, None)
    assert refused.status_code == 412 and "--client" in refused.get_json()["detail"]
    assert _op(client, "open", {"app": "terminal", "client": "ghost"}, None).status_code == 404
    # An argument the op itself needs is reported before any client is looked for.
    assert _op(client, "load", {}, None).status_code == 400


def test_a_whole_app_refresh_reaches_every_client_and_needs_no_target(client: FlaskClient, app: Flask) -> None:
    first_queue = _register_client(app, "c1", "home")
    second_queue = _register_client(app, "c2", "home")
    window_id = _open_window(client, "terminal", "/?session=terminal-1").get_json()["window"]["id"]
    drain_messages(first_queue)
    drain_messages(second_queue)

    # With two clients connected and none named, a whole-app refresh still goes out to both, while a
    # one-window refresh cannot tell which client's page it means.
    everywhere = _op(client, "refresh", {"app": "terminal"}, None)
    assert everywhere.status_code == 200 and everywhere.get_json()["target_client_id"] is None
    assert _op(client, "refresh", {"window": window_id}, None).status_code == 412
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
def test_a_malformed_op_argument_is_a_400_naming_the_argument(
    client: FlaskClient, app: Flask, op: str, args: dict[str, Any], fragment: str
) -> None:
    _register_client(app, "c1", "home")
    refused = _op(client, op, args, _TERMINAL_REQUESTER)
    assert refused.status_code == 400
    assert fragment in refused.get_json()["detail"]
