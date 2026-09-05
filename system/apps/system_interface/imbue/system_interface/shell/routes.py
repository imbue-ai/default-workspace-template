"""The shell's HTTP routes: contracts.md sections 5 and 6 (the inventory endpoint lands in phase 8)."""

import json
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Final
from typing import assert_never

from app_instances.blueprint import answer_typed_error
from app_instances.blueprint import parse_request_body
from app_instances.errors import AppInstancesError
from app_instances.primitives import InstanceKey
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import ActionId
from app_manifest.primitives import AppName
from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.client_activity import find_client_id_for_instance
from imbue.system_interface.shell.client_activity import summarize_client_activity
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import ClientActivityReport
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import LayoutSaveRequest
from imbue.system_interface.shell.data_types import Shortcut
from imbue.system_interface.shell.data_types import TabInstanceReport
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import EverythingIsNotAProjectError
from imbue.system_interface.shell.errors import InvalidAddressError
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import LayoutNotFoundError
from imbue.system_interface.shell.errors import ProjectConflictError
from imbue.system_interface.shell.errors import ProjectNotFoundError
from imbue.system_interface.shell.errors import ProjectValueError
from imbue.system_interface.shell.errors import ShellError
from imbue.system_interface.shell.errors import UnknownAppError
from imbue.system_interface.shell.instance_relay import RelayOutcome
from imbue.system_interface.shell.instance_relay import relay_create
from imbue.system_interface.shell.instance_relay import relay_delete
from imbue.system_interface.shell.instance_relay import relay_location
from imbue.system_interface.shell.instance_relay import relay_rename
from imbue.system_interface.shell.layout_ops import SELF_ADDRESS
from imbue.system_interface.shell.layout_ops import is_broadcasting_op
from imbue.system_interface.shell.layout_ops import is_known_op
from imbue.system_interface.shell.layout_ops import is_mutating_op
from imbue.system_interface.shell.layout_ops import layout_inspect
from imbue.system_interface.shell.layout_ops import layout_list
from imbue.system_interface.shell.layout_ops import layout_views
from imbue.system_interface.shell.layout_ops import view_display_name
from imbue.system_interface.shell.layouts import layout_wire_json
from imbue.system_interface.shell.liveness import SupervisorProgramActionError
from imbue.system_interface.shell.liveness import start_supervisor_program
from imbue.system_interface.shell.liveness import stop_supervisor_program
from imbue.system_interface.shell.liveness import supervisor_socket_path
from imbue.system_interface.shell.primitives import Address
from imbue.system_interface.shell.primitives import AppLifecycleAction
from imbue.system_interface.shell.primitives import ClientActivityKind
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.shell.primitives import EVERYTHING_VIEW_ID
from imbue.system_interface.shell.primitives import ProjectId
from imbue.system_interface.shell.primitives import TabId
from imbue.system_interface.shell.primitives import ViewId
from imbue.system_interface.shell.primitives import address_for
from imbue.system_interface.shell.primitives import is_everything_view
from imbue.system_interface.shell.projects import project_wire_json
from imbue.system_interface.shell.projects import seed_shortcuts
from imbue.system_interface.shell.projects import validated_shortcut
from imbue.system_interface.shell.state import ShellState

LOOPBACK_CLIENT_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})
# The app whose instances agents are: an agent-initiated op is attributed to the client that
# last messaged that agent's chat instance.
CHAT_APP_NAME_FOR_ATTRIBUTION: Final[str] = "chat"

HTTP_OK: Final[int] = 200
HTTP_CREATED: Final[int] = 201
HTTP_NO_CONTENT: Final[int] = 204
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_PRECONDITION_FAILED: Final[int] = 412
HTTP_INTERNAL_ERROR: Final[int] = 500
HTTP_BAD_GATEWAY: Final[int] = 502


class ProjectMetadataRequest(FrozenModel):
    """The body of project create and settings."""

    name: str = Field(description="The display name")
    color: str = Field(description="'#RRGGBB'")
    glyph: int = Field(description="The glyph index")


class ProjectTabRequest(FrozenModel):
    """The body of the tab-set routes."""

    address: Address = Field(description="The instance to add or remove")


class ProjectShortcutRequest(FrozenModel):
    """The body of the shortcut set route."""

    app: AppName = Field(description="The app")
    action: ActionId = Field(description="The action")
    mode: ShortcutMode = Field(description="focus or new")


class ProjectShortcutRemoveRequest(FrozenModel):
    """The body of the shortcut remove route."""

    app: AppName = Field(description="The app")
    action: ActionId = Field(description="The action")


def _detail(message: str, status_code: int) -> ResponseReturnValue:
    return jsonify({"detail": message}), status_code


def _answer_shell_error(error: ShellError) -> ResponseReturnValue:
    match error:
        case ProjectNotFoundError() | LayoutNotFoundError() | UnknownAppError() | EverythingIsNotAProjectError():
            return _detail(str(error), HTTP_NOT_FOUND)
        case ProjectConflictError():
            return _detail(str(error), HTTP_CONFLICT)
        case ProjectValueError() | InvalidAddressError() | InvalidShellValueError() | AppLifecycleRefusedError():
            return _detail(str(error), HTTP_BAD_REQUEST)
        case _:
            logger.opt(exception=error).error("Failed to serve a shell request")
            return _detail(str(error), HTTP_INTERNAL_ERROR)


def _require_loopback() -> ResponseReturnValue | None:
    if (request.remote_addr or "") not in LOOPBACK_CLIENT_HOSTS:
        return _detail("this route is only callable from loopback", HTTP_FORBIDDEN)
    return None


def _project_id(raw: str) -> ProjectId:
    if is_everything_view(raw):
        raise EverythingIsNotAProjectError(f"{EVERYTHING_VIEW_ID!r} is a view, not a project")
    return ProjectId(raw)


def _relay_response(outcome: RelayOutcome) -> Response:
    return Response(outcome.body, status=outcome.status_code, content_type=outcome.content_type)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _entry_or_raise(name: str) -> AppInventoryEntry:
    entry = _shell().inventory.entry(name)
    if entry is None:
        raise UnknownAppError(f"No registered app named {name!r}")
    return entry


# ---------- section 5: routes apps and scripts call ----------


def app_changed(name: str) -> ResponseReturnValue:
    refusal = _require_loopback()
    if refusal is not None:
        return refusal
    if not _shell().inventory.nudge(name):
        return _detail(f"No registered app named {name!r}", HTTP_NOT_FOUND)
    return "", HTTP_NO_CONTENT


def tab_instance(tab_id: str) -> ResponseReturnValue:
    refusal = _require_loopback()
    if refusal is not None:
        return refusal
    report = parse_request_body(TabInstanceReport)
    shell = _shell()
    found = shell.layouts.find_tab(TabId(tab_id))
    if not found:
        raise LayoutNotFoundError(f"No tab {tab_id!r} in any client layout")
    for stored, panel_id in found:
        if stored.layout.tabs[panel_id].address.app != report.app:
            return _detail(
                f"tab {tab_id!r} shows {stored.layout.tabs[panel_id].address}, not the app {report.app!r}",
                HTTP_BAD_REQUEST,
            )
    address = address_for(report.app, None if report.key == "" else InstanceKey(report.key))
    for stored in shell.layouts.rebind_tab(TabId(tab_id), address, _now()):
        if not is_everything_view(stored.view_id):
            shell.projects.add_tab(stored.view_id, address)
        shell.broadcaster.broadcast_tab_rebound(str(stored.client_id), str(stored.view_id), tab_id, str(address))
    shell.broadcast_projects_updated()
    shell.inventory.refetch_now(str(report.app))
    return "", HTTP_NO_CONTENT


def client_activity_route() -> ResponseReturnValue:
    refusal = _require_loopback()
    if refusal is not None:
        return refusal
    report = parse_request_body(ClientActivityReport)
    shell = _shell()
    match report.kind:
        case ClientActivityKind.MESSAGE:
            shell.activity.append_message(
                str(report.client_id),
                report.device_kind.value,
                str(report.view_id),
                report.app,
                report.key,
                report.text,
            )
        case ClientActivityKind.VIEW_SWITCH:
            shell.activity.append_view_switch(
                str(report.client_id), report.device_kind.value, report.from_view_id, str(report.view_id)
            )
        case _ as unreachable:
            assert_never(unreachable)
    return "", HTTP_NO_CONTENT


# ---------- section 6: the relay ----------


def relay_create_route(name: str) -> ResponseReturnValue:
    entry = _entry_or_raise(name)
    outcome = relay_create(_shell().http_client, entry, request.get_data())
    if outcome.status_code < HTTP_BAD_REQUEST:
        _shell().inventory.refetch_now(name)
    return _relay_response(outcome)


def relay_delete_route(name: str, key: str) -> ResponseReturnValue:
    entry = _entry_or_raise(name)
    outcome = relay_delete(_shell().http_client, entry, key)
    if outcome.status_code < HTTP_BAD_REQUEST:
        _shell().inventory.refetch_now(name)
    return _relay_response(outcome)


def relay_rename_route(name: str, key: str) -> ResponseReturnValue:
    entry = _entry_or_raise(name)
    outcome = relay_rename(_shell().http_client, entry, key, request.get_data())
    if outcome.status_code < HTTP_BAD_REQUEST:
        _shell().inventory.refetch_now(name)
    return _relay_response(outcome)


def relay_location_route(name: str, key: str) -> ResponseReturnValue:
    entry = _entry_or_raise(name)
    outcome = relay_location(_shell().http_client, entry, key, request.get_data())
    if outcome.status_code < HTTP_BAD_REQUEST:
        _shell().inventory.refetch_now(name)
    return _relay_response(outcome)


# ---------- section 6: stop and start ----------


def _lifecycle(name: str, action: AppLifecycleAction) -> ResponseReturnValue:
    shell = _shell()
    entry = _entry_or_raise(name)
    program = entry.row.program or ""
    if not program:
        raise AppLifecycleRefusedError(
            f"App {name!r} has no supervised program registered, so it cannot be stopped or started from the workspace"
        )
    # A critical app is never stopped from here, and neither is any row running inside a
    # critical app's program (the chat row shares the shell's program until phase 10).
    critical_programs = {
        other.row.program for other in shell.inventory.entries() if other.row.critical and other.row.program
    }
    if entry.row.critical or program in critical_programs:
        raise AppLifecycleRefusedError(
            f"App {name!r} is critical to the workspace and cannot be stopped or started here"
        )
    try:
        match action:
            case AppLifecycleAction.STOP:
                stop_supervisor_program(program, supervisor_socket_path())
            case AppLifecycleAction.START:
                start_supervisor_program(program, supervisor_socket_path())
            case _ as unreachable:
                assert_never(unreachable)
    except SupervisorProgramActionError as e:
        return _detail(str(e), HTTP_BAD_GATEWAY)
    logger.info("{} app {} (program {})", "Stopped" if action is AppLifecycleAction.STOP else "Started", name, program)
    shell.inventory.refresh_liveness()
    refreshed = shell.inventory.entry(name)
    return jsonify({"name": name, "is_running": refreshed.is_running if refreshed is not None else False})


def stop_app(name: str) -> ResponseReturnValue:
    return _lifecycle(name, AppLifecycleAction.STOP)


def start_app(name: str) -> ResponseReturnValue:
    return _lifecycle(name, AppLifecycleAction.START)


# ---------- section 6: projects ----------


def list_projects() -> ResponseReturnValue:
    return jsonify({"projects": [project_wire_json(project) for project in _shell().projects.list_projects()]})


def create_project() -> ResponseReturnValue:
    body = parse_request_body(ProjectMetadataRequest)
    shell = _shell()
    shortcuts = seed_shortcuts([entry.row for entry in shell.inventory.entries()])
    project = shell.projects.create_project(body.name, body.color, body.glyph, shortcuts)
    shell.broadcast_projects_updated()
    return jsonify(project_wire_json(project)), HTTP_CREATED


def update_project_settings(project_id: str) -> ResponseReturnValue:
    body = parse_request_body(ProjectMetadataRequest)
    shell = _shell()
    project = shell.projects.update_project_settings(_project_id(project_id), body.name, body.color, body.glyph)
    shell.broadcast_projects_updated()
    return jsonify(project_wire_json(project))


def delete_project(project_id: str) -> ResponseReturnValue:
    shell = _shell()
    fallback = shell.projects.delete_project(_project_id(project_id))
    shell.layouts.delete_view_layouts(project_id)
    logger.info("Deleted project {} (fallback {})", project_id, fallback)
    shell.broadcast_projects_updated()
    shell.delete_unreferenced_instances()
    return jsonify({"fallback_view_id": str(fallback)})


def add_project_tab(project_id: str) -> ResponseReturnValue:
    body = parse_request_body(ProjectTabRequest)
    shell = _shell()
    project = shell.projects.add_tab(_project_id(project_id), body.address)
    shell.broadcast_projects_updated()
    return jsonify(project_wire_json(project))


def remove_project_tab(project_id: str) -> ResponseReturnValue:
    body = parse_request_body(ProjectTabRequest)
    shell = _shell()
    project = shell.projects.remove_tab(_project_id(project_id), body.address)
    shell.broadcast_projects_updated()
    shell.delete_unreferenced_instances()
    return jsonify(project_wire_json(project))


def set_project_shortcut(project_id: str) -> ResponseReturnValue:
    body = parse_request_body(ProjectShortcutRequest)
    shell = _shell()
    entry = shell.inventory.entry(str(body.app))
    shortcut = validated_shortcut(
        Shortcut(app=body.app, action=body.action, mode=body.mode), entry.row if entry else None
    )
    project = shell.projects.set_shortcut(_project_id(project_id), shortcut)
    shell.broadcast_projects_updated()
    return jsonify(project_wire_json(project))


def remove_project_shortcut(project_id: str) -> ResponseReturnValue:
    body = parse_request_body(ProjectShortcutRemoveRequest)
    shell = _shell()
    project = shell.projects.remove_shortcut(_project_id(project_id), str(body.app), str(body.action))
    shell.broadcast_projects_updated()
    return jsonify(project_wire_json(project))


# ---------- section 6: layouts ----------


def get_layout(view_id: str) -> ResponseReturnValue:
    shell = _shell()
    view = ViewId(view_id)
    if not shell.projects.is_view_known(view):
        raise ProjectNotFoundError(view_id)
    client_id = ClientId(request.args.get("client", ""))
    client = shell.clients.get_client(client_id)
    raw_device = request.args.get("device", "")
    device_kind = (
        client.device_kind if client is not None else (DeviceKind(raw_device) if raw_device else DeviceKind.DESKTOP)
    )
    return jsonify(layout_wire_json(shell.layouts.read_layout(view, client_id, device_kind)))


def save_layout(view_id: str) -> ResponseReturnValue:
    body = parse_request_body(LayoutSaveRequest)
    shell = _shell()
    view = ViewId(view_id)
    if not shell.projects.is_view_known(view):
        raise ProjectNotFoundError(view_id)
    layout = LayoutRecord(dockview=body.dockview, tabs=body.tabs, device_kind=body.device_kind, updated_at=None)
    shell.layouts.save_layout(view, body.client_id, layout, _now())
    shell.delete_unreferenced_instances()
    return "", HTTP_NO_CONTENT


# ---------- the agent-facing broadcast endpoint ----------


def layout_broadcast() -> ResponseReturnValue:
    refusal = _require_loopback()
    if refusal is not None:
        return refusal
    try:
        body = json.loads(request.get_data())
    except ValueError as e:
        logger.opt(exception=e).warning("layout broadcast received invalid JSON body")
        return _detail("Invalid JSON in request body", HTTP_BAD_REQUEST)
    if not isinstance(body, dict):
        return _detail("Request body must be a JSON object", HTTP_BAD_REQUEST)
    op = body.get("op")
    args_raw = body.get("args", {})
    agent_id = str(body.get("agent_id") or request.headers.get("X-Mngr-Agent-Id") or "")
    if not isinstance(op, str) or not is_known_op(op):
        return _detail(f"Unknown layout op: {op!r}", HTTP_BAD_REQUEST)
    if not isinstance(args_raw, dict):
        return _detail("``args`` must be a JSON object", HTTP_BAD_REQUEST)
    return _dispatch_layout_op(_shell(), op, args_raw, agent_id)


def _shell() -> ShellState:
    return get_state().shell


def register_shell_routes(application: Flask) -> None:
    """Register every shell route of contracts.md sections 5 and 6 on ``application``."""
    application.register_error_handler(ShellError, _answer_shell_error)
    application.register_error_handler(AppInstancesError, answer_typed_error)
    application.add_url_rule(
        "/api/apps/<name>/changed", view_func=app_changed, methods=["POST"], endpoint="app_changed"
    )
    application.add_url_rule(
        "/api/tabs/<tab_id>/instance", view_func=tab_instance, methods=["POST"], endpoint="tab_instance"
    )
    application.add_url_rule(
        "/api/client-activity", view_func=client_activity_route, methods=["POST"], endpoint="client_activity_route"
    )
    application.add_url_rule(
        "/api/apps/<name>/instances", view_func=relay_create_route, methods=["POST"], endpoint="relay_create_route"
    )
    application.add_url_rule(
        "/api/apps/<name>/instances/<key>/delete",
        view_func=relay_delete_route,
        methods=["POST"],
        endpoint="relay_delete_route",
    )
    application.add_url_rule(
        "/api/apps/<name>/instances/<key>/rename",
        view_func=relay_rename_route,
        methods=["POST"],
        endpoint="relay_rename_route",
    )
    application.add_url_rule(
        "/api/apps/<name>/instances/<key>/location",
        view_func=relay_location_route,
        methods=["POST"],
        endpoint="relay_location_route",
    )
    application.add_url_rule("/api/apps/<name>/stop", view_func=stop_app, methods=["POST"], endpoint="stop_app")
    application.add_url_rule("/api/apps/<name>/start", view_func=start_app, methods=["POST"], endpoint="start_app")
    application.add_url_rule("/api/projects", view_func=list_projects, methods=["GET"], endpoint="list_projects")
    application.add_url_rule("/api/projects", view_func=create_project, methods=["POST"], endpoint="create_project")
    application.add_url_rule(
        "/api/projects/<project_id>/settings",
        view_func=update_project_settings,
        methods=["POST"],
        endpoint="update_project_settings",
    )
    application.add_url_rule(
        "/api/projects/<project_id>/delete", view_func=delete_project, methods=["POST"], endpoint="delete_project"
    )
    application.add_url_rule(
        "/api/projects/<project_id>/tabs", view_func=add_project_tab, methods=["POST"], endpoint="add_project_tab"
    )
    application.add_url_rule(
        "/api/projects/<project_id>/tabs/remove",
        view_func=remove_project_tab,
        methods=["POST"],
        endpoint="remove_project_tab",
    )
    application.add_url_rule(
        "/api/projects/<project_id>/shortcuts",
        view_func=set_project_shortcut,
        methods=["POST"],
        endpoint="set_project_shortcut",
    )
    application.add_url_rule(
        "/api/projects/<project_id>/shortcuts/remove",
        view_func=remove_project_shortcut,
        methods=["POST"],
        endpoint="remove_project_shortcut",
    )
    application.add_url_rule("/api/layouts/<view_id>", view_func=get_layout, methods=["GET"], endpoint="get_layout")
    application.add_url_rule("/api/layouts/<view_id>", view_func=save_layout, methods=["POST"], endpoint="save_layout")
    application.add_url_rule(
        "/api/layout/broadcast", view_func=layout_broadcast, methods=["POST"], endpoint="layout_broadcast"
    )


def _clients_by_view(shell: ShellState) -> dict[str, list[dict[str, str]]]:
    clients_by_view: dict[str, list[dict[str, str]]] = {}
    for info in shell.broadcaster.get_connected_client_infos():
        clients_by_view.setdefault(info["active_view"], []).append(
            {"id": info["client_id"], "device_kind": info["device_kind"]}
        )
    return clients_by_view


def _resolve_view(shell: ShellState, args_raw: dict[str, Any]) -> tuple[str | None, ResponseReturnValue | None]:
    """The view an op targets: ``args.view`` (a project name or id, or Everything), else the connected client's view."""
    requested = args_raw.get("view")
    projects = shell.projects.list_projects()
    if isinstance(requested, str) and requested:
        if requested.strip().lower() == EVERYTHING_VIEW_ID:
            return EVERYTHING_VIEW_ID, None
        for project in projects:
            if project.id == requested or project.name.strip().lower() == requested.strip().lower():
                return str(project.id), None
        known = ", ".join([project.name for project in projects] + ["Everything"])
        return None, _detail(f"View {requested!r} not found (known views: {known})", HTTP_NOT_FOUND)
    connected_views = {info["active_view"] for info in shell.broadcaster.get_connected_client_infos()}
    if len(connected_views) == 1:
        return next(iter(connected_views)), None
    clients = shell.clients.list_clients()
    if clients:
        return str(clients[0].active_view), None
    return None, None


def _resolve_client(shell: ShellState, args_raw: dict[str, Any], agent_id: str, view_id: str | None) -> str | None:
    """The client an op addresses: ``args.client``, else the client that last messaged the requesting agent, else the one connected client on the view."""
    explicit = args_raw.get("client")
    if isinstance(explicit, str) and explicit:
        return explicit
    # The requester is an agent, and agents are the chat app's instances (keyed by agent id).
    attributed = find_client_id_for_instance(shell.activity.read_events(), CHAT_APP_NAME_FOR_ATTRIBUTION, agent_id)
    if attributed is not None:
        return attributed
    connected = [
        info
        for info in shell.broadcaster.get_connected_client_infos()
        if view_id is None or info["active_view"] == view_id
    ]
    if len(connected) == 1:
        return connected[0]["client_id"]
    return None


def _dispatch_layout_op(shell: ShellState, op: str, args_raw: dict[str, Any], agent_id: str) -> ResponseReturnValue:
    match op:
        case "list":
            return _op_list(shell, args_raw, agent_id)
        case "inspect":
            return _op_inspect(shell, args_raw, agent_id)
        case "views":
            return _op_views(shell, agent_id)
        case "context":
            return _op_context(shell, agent_id)
        case "load":
            return _op_load(shell, args_raw, agent_id)
        case _:
            return _op_broadcast(shell, op, args_raw, agent_id)


def _op_list(shell: ShellState, args_raw: dict[str, Any], agent_id: str) -> ResponseReturnValue:
    view_id, error = _resolve_view(shell, args_raw)
    if error is not None:
        return error
    layouts = [stored for stored in shell.layouts.all_client_layouts() if view_id is None or stored.view_id == view_id]
    listing = layout_list(shell.inventory.entries(), layouts)
    logger.info("layout op=list agent_id={} view={} apps={}", agent_id, view_id, len(listing))
    return jsonify({"ok": True, "view_id": view_id, "apps": listing})


def _op_inspect(shell: ShellState, args_raw: dict[str, Any], agent_id: str) -> ResponseReturnValue:
    view_id, error = _resolve_view(shell, args_raw)
    if error is not None:
        return error
    client_id = _resolve_client(shell, args_raw, agent_id, view_id)
    layout = None
    if view_id is not None and client_id is not None:
        client = shell.clients.get_client(client_id)
        device_kind = client.device_kind if client is not None else DeviceKind.DESKTOP
        layout = shell.layouts.read_layout(view_id, client_id, device_kind)
    title_by_address = {
        str(entry.address_of(instance)): instance.title
        for entry in shell.inventory.entries()
        for instance in entry.instances
    }
    summary = layout_inspect(layout, title_by_address)
    logger.info(
        "layout op=inspect agent_id={} view={} client={} panels={}",
        agent_id,
        view_id,
        client_id,
        len(summary["panels"]),
    )
    return jsonify({"ok": True, "view_id": view_id, "client_id": client_id, "layout": summary})


def _op_views(shell: ShellState, agent_id: str) -> ResponseReturnValue:
    projects = shell.projects.list_projects()
    everything_tabs = [
        address for entry in shell.inventory.entries() if not entry.row.internal for address in entry.addresses()
    ]
    views = layout_views(projects, everything_tabs, _clients_by_view(shell))
    logger.info("layout op=views agent_id={} views={}", agent_id, len(views))
    return jsonify({"ok": True, "views": views})


def _op_context(shell: ShellState, agent_id: str) -> ResponseReturnValue:
    events = shell.activity.read_events()
    connected_infos = shell.broadcaster.get_connected_client_infos()
    live_view_by_client_id = {info["client_id"]: info["active_view"] for info in connected_infos}
    clients = summarize_client_activity(events, set(live_view_by_client_id))
    for client_summary in clients:
        live_view = live_view_by_client_id.get(client_summary["client_id"])
        if live_view:
            client_summary["active_view"] = live_view
    logger.info("layout op=context agent_id={} clients={}", agent_id, len(clients))
    return jsonify({"ok": True, "clients": clients})


def _op_load(shell: ShellState, args_raw: dict[str, Any], agent_id: str) -> ResponseReturnValue:
    requested = args_raw.get("view")
    if not isinstance(requested, str) or not requested:
        return _detail("'load' requires a view name in args.view", HTTP_BAD_REQUEST)
    view_id, error = _resolve_view(shell, args_raw)
    if error is not None:
        return error
    if view_id is None:
        return _detail("Failed to resolve the requested view", HTTP_INTERNAL_ERROR)
    target_client_id = _resolve_client(shell, args_raw, agent_id, None)
    display_name = view_display_name(view_id, shell.projects.list_projects())
    shell.broadcaster.broadcast_load_layout(view_id, display_name, target_client_id)
    logger.info("layout op=load agent_id={} view={} target_client={}", agent_id, view_id, target_client_id)
    return jsonify({"ok": True, "view_id": view_id, "target_client_id": target_client_id})


def _refuse_unregistered_address(shell: ShellState, args_raw: dict[str, Any]) -> ResponseReturnValue | None:
    """An addressed op names an instance or an app; the address must parse, and an app it names must be registered, before anything is broadcast."""
    raw_address = args_raw.get("address")
    if raw_address is None or raw_address == SELF_ADDRESS:
        return None
    try:
        address = Address(str(raw_address))
    except InvalidAddressError as e:
        return _detail(str(e), HTTP_BAD_REQUEST)
    if shell.inventory.entry(str(address.app)) is None:
        return _detail(f"No registered app named {address.app!r}", HTTP_NOT_FOUND)
    return None


def _no_client_on_view_response(
    shell: ShellState, op: str, args_raw: dict[str, Any], target_view: str | None
) -> ResponseReturnValue:
    """The 412 a mutating op gets when no connected client has the target view active."""
    connected_clients = shell.broadcaster.get_connected_client_infos()
    requested_view = args_raw.get("view") or target_view or "<no view>"
    logger.warning(
        "Layout op {!r} rejected (412): no connected client on view {!r}; connected clients: {}",
        op,
        requested_view,
        connected_clients,
    )
    client_summary = (
        ", ".join(
            f"{info['client_id']} (view={info['active_view']}, device={info['device_kind']})"
            for info in connected_clients
        )
        or "none"
    )
    return _detail(
        f"No connected client has view '{requested_view}' active. Ask the user to switch to it, "
        f"or run `layout.py load {requested_view!r}` first. Connected clients: {client_summary}.",
        HTTP_PRECONDITION_FAILED,
    )


def _broadcast_mutating_op(
    shell: ShellState, op: str, args_raw: dict[str, Any], agent_id: str
) -> ResponseReturnValue | None:
    """Broadcast a view-targeted op under the layout mutex; a 412 or 409 when it cannot land, None when it did."""
    target_view, error = _resolve_view(shell, args_raw)
    if error is not None:
        return error
    if target_view is None or not shell.broadcaster.has_client_on_view(target_view):
        return _no_client_on_view_response(shell, op, args_raw, target_view)
    broadcast_args = {key: value for key, value in args_raw.items() if key != "view"}
    holder = shell.layout_mutex.try_acquire(agent_id, op, args_raw)
    if holder is not None:
        return jsonify(
            {
                "detail": (
                    f"Another layout op is in flight: agent_id={holder['agent_id']} op={holder['operation']}. "
                    "Retry after the mutex TTL elapses."
                ),
                "retry_after_ms": shell.layout_mutex.retry_after_ms(),
                "in_flight": holder,
            }
        ), HTTP_CONFLICT
    try:
        shell.broadcaster.broadcast_layout_op(op, broadcast_args, requester_agent_id=agent_id, target_view=target_view)
    finally:
        shell.layout_mutex.release(agent_id, op)
    return None


def _op_broadcast(shell: ShellState, op: str, args_raw: dict[str, Any], agent_id: str) -> ResponseReturnValue:
    """Every op the connected clients carry out: checked here, then sent as a ``layout_op`` message."""
    if not is_broadcasting_op(op):
        return _detail(f"Op {op!r} has no broadcast handler", HTTP_INTERNAL_ERROR)
    refusal = _refuse_unregistered_address(shell, args_raw)
    if refusal is not None:
        return refusal
    if is_mutating_op(op):
        failure = _broadcast_mutating_op(shell, op, args_raw, agent_id)
        if failure is not None:
            return failure
    else:
        shell.broadcaster.broadcast_layout_op(op, args_raw, requester_agent_id=agent_id)
    logger.info("layout op={} agent_id={} args={}", op, agent_id, args_raw)
    return jsonify({"ok": True})
