"""The shell's HTTP routes: contracts.md sections 5, 6, 9, and the op route of section 12."""

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
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.client_activity import find_client_id_for_instance
from imbue.system_interface.shell.client_activity import summarize_client_activity
from imbue.system_interface.shell.clients import client_wire_json
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import ClientActivityReport
from imbue.system_interface.shell.data_types import LayoutRecord
from imbue.system_interface.shell.data_types import LayoutSaveRequest
from imbue.system_interface.shell.data_types import Shortcut
from imbue.system_interface.shell.data_types import TabInstanceReport
from imbue.system_interface.shell.data_types import effective_actions
from imbue.system_interface.shell.dockview_document import Direction
from imbue.system_interface.shell.dockview_document import Placement
from imbue.system_interface.shell.dockview_document import add_panel
from imbue.system_interface.shell.dockview_document import focus_panel
from imbue.system_interface.shell.dockview_document import move_panel
from imbue.system_interface.shell.dockview_document import panel_id_for_address
from imbue.system_interface.shell.dockview_document import remove_panel
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.errors import EverythingIsNotAProjectError
from imbue.system_interface.shell.errors import InstanceCreateRefusedError
from imbue.system_interface.shell.errors import InstanceNotListedError
from imbue.system_interface.shell.errors import InvalidAddressError
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import LayoutNotFoundError
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.errors import NoTargetClientError
from imbue.system_interface.shell.errors import PanelNotFoundError
from imbue.system_interface.shell.errors import ProjectConflictError
from imbue.system_interface.shell.errors import ProjectNotFoundError
from imbue.system_interface.shell.errors import ProjectValueError
from imbue.system_interface.shell.errors import ShellError
from imbue.system_interface.shell.errors import StaleLayoutSaveError
from imbue.system_interface.shell.errors import SupervisorProgramActionError
from imbue.system_interface.shell.errors import UnknownAppError
from imbue.system_interface.shell.instance_relay import RelayOutcome
from imbue.system_interface.shell.instance_relay import relay_create
from imbue.system_interface.shell.instance_relay import relay_delete
from imbue.system_interface.shell.instance_relay import relay_location
from imbue.system_interface.shell.instance_relay import relay_rename
from imbue.system_interface.shell.inventory import build_inventory_document
from imbue.system_interface.shell.layout_ops import DocumentOpArguments
from imbue.system_interface.shell.layout_ops import SELF_ADDRESS
from imbue.system_interface.shell.layout_ops import is_addressed_op
from imbue.system_interface.shell.layout_ops import is_creating_op
from imbue.system_interface.shell.layout_ops import is_document_op
from imbue.system_interface.shell.layout_ops import is_known_op
from imbue.system_interface.shell.layout_ops import is_transient_op
from imbue.system_interface.shell.layout_ops import layout_inspect
from imbue.system_interface.shell.layouts import layout_wire_json
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
from imbue.system_interface.shell.primitives import mint_group_id
from imbue.system_interface.shell.primitives import mint_tab_id
from imbue.system_interface.shell.projects import project_wire_json
from imbue.system_interface.shell.projects import seed_shortcuts
from imbue.system_interface.shell.projects import validated_shortcut
from imbue.system_interface.shell.state import ShellState

LOOPBACK_CLIENT_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

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
        case (
            ProjectNotFoundError()
            | LayoutNotFoundError()
            | UnknownAppError()
            | EverythingIsNotAProjectError()
            | ClientNotFoundError()
            | PanelNotFoundError()
            | InstanceNotListedError()
        ):
            return _detail(str(error), HTTP_NOT_FOUND)
        case ProjectConflictError() | StaleLayoutSaveError():
            return _detail(str(error), HTTP_CONFLICT)
        case (
            ProjectValueError()
            | InvalidAddressError()
            | InvalidShellValueError()
            | AppLifecycleRefusedError()
            | LayoutOpError()
        ):
            return _detail(str(error), HTTP_BAD_REQUEST)
        case NoTargetClientError():
            return _detail(str(error), HTTP_PRECONDITION_FAILED)
        case InstanceCreateRefusedError():
            return _detail(error.detail, error.status_code)
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


def _device_kind_from_query(raw: str) -> DeviceKind:
    try:
        return DeviceKind(raw)
    except ValueError as e:
        raise InvalidShellValueError(f"invalid device kind {raw!r}") from e


def _entry_or_raise(name: str) -> AppInventoryEntry:
    entry = _shell().inventory.entry(name)
    if entry is None:
        raise UnknownAppError(f"No registered app named {name!r}")
    return entry


def _instance_key_or_raise(raw_key: str) -> InstanceKey:
    """The key of a keyed relay route, checked against the key rule before the app is consulted (a 400 otherwise)."""
    return InstanceKey(raw_key)


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
    for stored in shell.rebind_tab(TabId(tab_id), address):
        if not is_everything_view(stored.view_id):
            shell.projects.add_tab(stored.view_id, address)
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
                str(report.client_id),
                report.device_kind.value,
                report.from_view_id,
                str(report.view_id),
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
    _instance_key_or_raise(key)
    outcome = relay_delete(_shell().http_client, entry, key)
    if outcome.status_code < HTTP_BAD_REQUEST:
        _shell().inventory.refetch_now(name)
    return _relay_response(outcome)


def relay_rename_route(name: str, key: str) -> ResponseReturnValue:
    entry = _entry_or_raise(name)
    _instance_key_or_raise(key)
    outcome = relay_rename(_shell().http_client, entry, key, request.get_data())
    if outcome.status_code < HTTP_BAD_REQUEST:
        _shell().inventory.refetch_now(name)
    return _relay_response(outcome)


def relay_location_route(name: str, key: str) -> ResponseReturnValue:
    entry = _entry_or_raise(name)
    _instance_key_or_raise(key)
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
    logger.info(
        "{} app {} (program {})",
        "Stopped" if action is AppLifecycleAction.STOP else "Started",
        name,
        program,
    )
    shell.inventory.refresh_liveness()
    refreshed = shell.inventory.entry(name)
    return jsonify(
        {
            "name": name,
            "is_running": refreshed.is_running if refreshed is not None else False,
        }
    )


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
        Shortcut(app=body.app, action=body.action, mode=body.mode),
        entry.row if entry else None,
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
        client.device_kind
        if client is not None
        else (_device_kind_from_query(raw_device) if raw_device else DeviceKind.DESKTOP)
    )
    return jsonify(layout_wire_json(shell.layouts.read_layout(view, client_id, device_kind)))


def save_layout(view_id: str) -> ResponseReturnValue:
    body = parse_request_body(LayoutSaveRequest)
    shell = _shell()
    view = ViewId(view_id)
    if not shell.projects.is_view_known(view):
        raise ProjectNotFoundError(view_id)
    saved = shell.save_browser_layout(view, body)
    # The stamp is spelled as the layout route spells it, so the window compares like with like.
    return jsonify({"updated_at": layout_wire_json(saved)["updated_at"] if saved is not None else None})


# ---------- sections 6 and 9: clients and the inventory ----------


def list_clients() -> ResponseReturnValue:
    shell = _shell()
    connected = shell.broadcaster.connected_client_ids()
    return jsonify(
        {"clients": [client_wire_json(client, str(client.id) in connected) for client in shell.clients.list_clients()]}
    )


def inventory_document() -> ResponseReturnValue:
    shell = _shell()
    clients = shell.clients.list_clients()
    docked_by_client_id = {
        str(client.id): [
            tab.address
            for tab in shell.layouts.read_layout(client.active_view, client.id, client.device_kind).tabs.values()
        ]
        for client in clients
    }
    return jsonify(
        build_inventory_document(
            shell.inventory.entries(),
            shell.projects.list_projects(),
            clients,
            shell.broadcaster.connected_client_ids(),
            docked_by_client_id,
        )
    )


# ---------- the agent-facing op route (contracts.md section 12) ----------


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
    requester = _requester_address(str(body.get("requester") or ""))
    if not isinstance(op, str) or not is_known_op(op):
        return _detail(f"Unknown layout op: {op!r}", HTTP_BAD_REQUEST)
    if not isinstance(args_raw, dict):
        return _detail("``args`` must be a JSON object", HTTP_BAD_REQUEST)
    return _dispatch_layout_op(_shell(), op, args_raw, requester)


def _shell() -> ShellState:
    return get_state().shell


def register_shell_routes(application: Flask) -> None:
    """Register every shell route of contracts.md sections 5, 6, 9, and 12 on ``application``."""
    application.register_error_handler(ShellError, _answer_shell_error)
    application.register_error_handler(AppInstancesError, answer_typed_error)
    application.add_url_rule(
        "/api/apps/<name>/changed",
        view_func=app_changed,
        methods=["POST"],
        endpoint="app_changed",
    )
    application.add_url_rule(
        "/api/tabs/<tab_id>/instance",
        view_func=tab_instance,
        methods=["POST"],
        endpoint="tab_instance",
    )
    application.add_url_rule(
        "/api/client-activity",
        view_func=client_activity_route,
        methods=["POST"],
        endpoint="client_activity_route",
    )
    application.add_url_rule(
        "/api/apps/<name>/instances",
        view_func=relay_create_route,
        methods=["POST"],
        endpoint="relay_create_route",
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
    application.add_url_rule(
        "/api/apps/<name>/stop",
        view_func=stop_app,
        methods=["POST"],
        endpoint="stop_app",
    )
    application.add_url_rule(
        "/api/apps/<name>/start",
        view_func=start_app,
        methods=["POST"],
        endpoint="start_app",
    )
    application.add_url_rule(
        "/api/projects",
        view_func=list_projects,
        methods=["GET"],
        endpoint="list_projects",
    )
    application.add_url_rule(
        "/api/projects",
        view_func=create_project,
        methods=["POST"],
        endpoint="create_project",
    )
    application.add_url_rule(
        "/api/projects/<project_id>/settings",
        view_func=update_project_settings,
        methods=["POST"],
        endpoint="update_project_settings",
    )
    application.add_url_rule(
        "/api/projects/<project_id>/delete",
        view_func=delete_project,
        methods=["POST"],
        endpoint="delete_project",
    )
    application.add_url_rule(
        "/api/projects/<project_id>/tabs",
        view_func=add_project_tab,
        methods=["POST"],
        endpoint="add_project_tab",
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
    application.add_url_rule(
        "/api/layouts/<view_id>",
        view_func=get_layout,
        methods=["GET"],
        endpoint="get_layout",
    )
    application.add_url_rule(
        "/api/layouts/<view_id>",
        view_func=save_layout,
        methods=["POST"],
        endpoint="save_layout",
    )
    application.add_url_rule("/api/clients", view_func=list_clients, methods=["GET"], endpoint="list_clients")
    application.add_url_rule(
        "/api/inventory",
        view_func=inventory_document,
        methods=["GET"],
        endpoint="inventory_document",
    )
    application.add_url_rule(
        "/api/layout/broadcast",
        view_func=layout_broadcast,
        methods=["POST"],
        endpoint="layout_broadcast",
    )


# ---------- resolving the target: which view, which client ----------


def _find_view(shell: ShellState, requested: str) -> tuple[str | None, ResponseReturnValue | None]:
    """The view a name or id names: a project's name or id, or Everything; a 404 naming the known views otherwise."""
    projects = shell.projects.list_projects()
    if requested.strip().lower() == EVERYTHING_VIEW_ID:
        return EVERYTHING_VIEW_ID, None
    for project in projects:
        if project.id == requested or project.name.strip().lower() == requested.strip().lower():
            return str(project.id), None
    known = ", ".join([project.name for project in projects] + ["Everything"])
    return None, _detail(f"View {requested!r} not found (known views: {known})", HTTP_NOT_FOUND)


def _requested_view(args_raw: dict[str, Any]) -> str | None:
    """The view an op names in ``args.view`` (a project's name or id, or Everything), None when it names none."""
    requested = args_raw.get("view")
    return requested if isinstance(requested, str) and requested else None


def _resolve_view(shell: ShellState, args_raw: dict[str, Any]) -> tuple[str | None, ResponseReturnValue | None]:
    """The view ``inspect`` reads: ``args.view``, else the one connected view, else the newest client's; None when
    nothing settles it (the read then answers with no arrangement)."""
    requested = _requested_view(args_raw)
    if requested is not None:
        return _find_view(shell, requested)
    connected_views = {info["active_view"] for info in shell.broadcaster.get_connected_client_infos()}
    if len(connected_views) == 1:
        return next(iter(connected_views)), None
    clients = shell.clients.list_clients()
    if clients:
        return str(clients[0].active_view), None
    return None, None


def _is_known_client(shell: ShellState, client_id: str) -> bool:
    return shell.clients.get_client(client_id) is not None or client_id in shell.broadcaster.connected_client_ids()


def _resolve_client(shell: ShellState, args_raw: dict[str, Any], requester: Address | None) -> ClientId | None:
    """The client an op addresses: ``args.client``, else the client that last messaged the requester's instance, else
    the one connected client; None when nothing settles it."""
    explicit = args_raw.get("client")
    if isinstance(explicit, str) and explicit:
        # Held to the client id rule before it names a layout file.
        client_id = ClientId(explicit)
        if not _is_known_client(shell, client_id):
            raise ClientNotFoundError(f"No client {client_id!r}: see `layout.py context` for the known clients")
        return client_id
    if requester is not None:
        attributed = find_client_id_for_instance(shell.activity.read_events(), str(requester.app), str(requester.key))
        if attributed is not None and _is_known_client(shell, attributed):
            return ClientId(attributed)
    connected = shell.broadcaster.connected_client_ids()
    if len(connected) == 1:
        return ClientId(next(iter(connected)))
    return None


def _require_client(shell: ShellState, args_raw: dict[str, Any], requester: Address | None) -> ClientId:
    """Exactly one client, or a 412 that lists the connected ones: an op is never applied to a guessed client."""
    client_id = _resolve_client(shell, args_raw, requester)
    if client_id is not None:
        return client_id
    connected_clients = shell.broadcaster.get_connected_client_infos()
    client_summary = (
        ", ".join(
            f"{info['client_id']} (view={info['active_view']}, device={info['device_kind']})"
            for info in connected_clients
        )
        or "none"
    )
    raise NoTargetClientError(
        "Could not tell which client this op is for: no client has messaged the requesting agent and "
        f"{len(connected_clients)} client(s) are connected. Pass --client <id> (see `layout.py context`). "
        f"Connected clients: {client_summary}."
    )


def _active_view_of_client(shell: ShellState, client_id: ClientId) -> str | None:
    record = shell.clients.get_client(client_id)
    if record is not None:
        return str(record.active_view)
    for info in shell.broadcaster.get_connected_client_infos():
        if info["client_id"] == client_id:
            return info["active_view"]
    return None


def _resolve_op_view(
    shell: ShellState, args_raw: dict[str, Any], client_id: ClientId
) -> tuple[str | None, ResponseReturnValue | None]:
    """The view a document op edits: ``args.view``, else the client's active view."""
    requested = _requested_view(args_raw)
    if requested is not None:
        return _find_view(shell, requested)
    active = _active_view_of_client(shell, client_id)
    if active is None:
        raise NoTargetClientError(f"Client {client_id!r} has no active view on record; pass --view <name>")
    return active, None


# ---------- the dispatch ----------


def _dispatch_layout_op(
    shell: ShellState, op: str, args_raw: dict[str, Any], requester: Address | None
) -> ResponseReturnValue:
    match op:
        case "inspect":
            return _op_inspect(shell, args_raw, requester)
        case "context":
            return _op_context(shell, requester)
        case "load":
            return _op_load(shell, args_raw, requester)
        case _ if is_document_op(op):
            return _op_document(shell, op, args_raw, requester)
        case _ if is_transient_op(op):
            return _op_transient(shell, op, args_raw, requester)
        case _:
            return _detail(f"Op {op!r} has no handler", HTTP_INTERNAL_ERROR)


def _title_by_address(shell: ShellState) -> dict[str, str]:
    return {
        str(entry.address_of(instance)): instance.title
        for entry in shell.inventory.entries()
        for instance in entry.instances
    }


def _op_inspect(shell: ShellState, args_raw: dict[str, Any], requester: Address | None) -> ResponseReturnValue:
    client_id = _resolve_client(shell, args_raw, requester)
    view_id: str | None
    if client_id is not None and _requested_view(args_raw) is None:
        view_id, error = _active_view_of_client(shell, client_id), None
    else:
        view_id, error = _resolve_view(shell, args_raw)
    if error is not None:
        return error
    layout = None
    if view_id is not None and client_id is not None:
        layout = shell.materialize_client_layout(ViewId(view_id), client_id)
    summary = layout_inspect(layout, _title_by_address(shell))
    logger.info(
        "layout op=inspect requester={} view={} client={} panels={}",
        requester,
        view_id,
        client_id,
        len(summary["panels"]),
    )
    return jsonify({"ok": True, "view_id": view_id, "client_id": client_id, "layout": summary})


def _op_context(shell: ShellState, requester: Address | None) -> ResponseReturnValue:
    clients = summarize_client_activity(shell.activity.read_events(), shell.broadcaster.get_connected_client_infos())
    logger.info("layout op=context requester={} clients={}", requester, len(clients))
    return jsonify({"ok": True, "clients": clients})


def _op_load(shell: ShellState, args_raw: dict[str, Any], requester: Address | None) -> ResponseReturnValue:
    requested = args_raw.get("view")
    if not isinstance(requested, str) or not requested:
        return _detail("'load' requires a view name in args.view", HTTP_BAD_REQUEST)
    view_id, error = _find_view(shell, requested)
    if error is not None or view_id is None:
        return error if error is not None else _detail("Failed to resolve the requested view", HTTP_INTERNAL_ERROR)
    client_id = _require_client(shell, args_raw, requester)
    shell.set_client_active_view(client_id, ViewId(view_id))
    logger.info(
        "layout op=load requester={} view={} target_client={}",
        requester,
        view_id,
        client_id,
    )
    return jsonify({"ok": True, "view_id": view_id, "target_client_id": str(client_id)})


# The keys that pick an op's target rather than describe the op; stripped before the op's own arguments are read.
_TARGET_ARG_KEYS: Final[frozenset[str]] = frozenset({"view", "client"})


def _op_only_args(args_raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args_raw.items() if key not in _TARGET_ARG_KEYS}


def _parse_document_arguments(args_raw: dict[str, Any]) -> DocumentOpArguments:
    try:
        return DocumentOpArguments.model_validate(_op_only_args(args_raw))
    except ValidationError as e:
        raise LayoutOpError(f"bad op arguments: {e.errors()[0]['msg']}") from e


def _requester_address(raw: str) -> Address | None:
    """The requester the op names (``layout.py`` sends the caller's own chat), or None for none or an unparseable one."""
    if not raw:
        return None
    try:
        return Address(raw)
    except InvalidAddressError as e:
        logger.debug("Ignored a requester that is not an address ({}): {}", raw, e)
        return None


def _resolve_op_address(raw: str, requester: Address | None) -> Address:
    """An op's address argument: ``self`` is the requester's own instance; anything else must parse."""
    if raw == SELF_ADDRESS:
        if requester is None:
            raise LayoutOpError("'self' names the requester's own chat, which needs MNGR_AGENT_ID to be set")
        return requester
    if not raw:
        raise LayoutOpError("this op needs an address")
    return Address(raw)


def _require_panel(layout: LayoutRecord, address: Address) -> str:
    panel_id = panel_id_for_address(layout, address)
    if panel_id is None:
        raise PanelNotFoundError(f"{address} is not open in this arrangement")
    return panel_id


def _create_action_id(entry: AppInventoryEntry, arguments: DocumentOpArguments) -> str:
    """The action a create runs: the one the op names, else the app's ``default_shortcut`` action when it declares
    that action, else its first declared action."""
    if arguments.action:
        return arguments.action
    declared = effective_actions(entry.row)
    default_shortcut = entry.row.default_shortcut
    if default_shortcut is not None and any(action.id == default_shortcut.action for action in declared):
        return str(default_shortcut.action)
    if declared:
        return str(declared[0].id)
    raise LayoutOpError(f"App {entry.row.name!r} declares no action to create an instance with")


class _CreatedInstance(FrozenModel):
    """The instance a create made, as the app's answer described it."""

    address: Address = Field(description="The new instance's address")
    title: str = Field(description="The title the app gave it, which the new panel takes")


def _create_through_relay(
    shell: ShellState, entry: AppInventoryEntry, arguments: DocumentOpArguments
) -> _CreatedInstance:
    """Run the app's action through the relay (the same route the browser uses) and answer the instance it made. The
    title comes from the app's answer rather than the inventory, which may not have listed the instance yet."""
    body = json.dumps(
        {
            "action": _create_action_id(entry, arguments),
            "params": dict(arguments.params),
        }
    ).encode()
    outcome = relay_create(shell.http_client, entry, body)
    if outcome.status_code >= HTTP_BAD_REQUEST:
        raise InstanceCreateRefusedError(outcome.status_code, _relay_detail(outcome))
    try:
        record = json.loads(outcome.body)["instance"]
        created = _CreatedInstance(
            address=address_for(entry.row.name, InstanceKey(str(record["key"]))),
            title=str(record["title"]),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise InstanceCreateRefusedError(
            HTTP_BAD_GATEWAY,
            f"App {entry.row.name!r} answered the create with an unreadable body",
        ) from e
    shell.inventory.refetch_now(str(entry.row.name))
    return created


def _relay_detail(outcome: RelayOutcome) -> str:
    try:
        parsed = json.loads(outcome.body)
    except ValueError:
        return outcome.body.decode(errors="replace")
    if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
        return parsed["detail"]
    return outcome.body.decode(errors="replace")


def _anchor_panel_id(layout: LayoutRecord, raw_anchor: str, requester: Address | None) -> str:
    """The panel a split or a move is relative to; ``self`` must be docked for that to mean anything."""
    address = _resolve_op_address(raw_anchor, requester)
    return _require_panel(layout, address)


def _anchored_placement(layout: LayoutRecord, arguments: DocumentOpArguments, requester: Address | None) -> Placement:
    """The placement a split or a move posts: relative to its anchor, in its direction."""
    return Placement(
        anchor_panel_id=_anchor_panel_id(layout, arguments.relative_to, requester),
        direction=arguments.direction,
        ratio=arguments.ratio,
        is_new_group=arguments.new_group,
        group_id=mint_group_id(),
    )


def _docking_placement(
    layout: LayoutRecord,
    op: str,
    arguments: DocumentOpArguments,
    requester: Address | None,
) -> Placement:
    """Where ``open`` and ``split`` dock: open lands beside the requester's own chat when it is docked (else beside
    the active group), tabbing into a group already there unless ``new_group``; split follows its anchor and direction."""
    if op == "split":
        return _anchored_placement(layout, arguments, requester)
    chat_panel = panel_id_for_address(layout, requester) if requester is not None else None
    return Placement(
        anchor_panel_id=chat_panel,
        direction=Direction.RIGHT,
        ratio=arguments.ratio,
        is_new_group=arguments.new_group,
        group_id=mint_group_id(),
    )


class _DocumentOpTarget(FrozenModel):
    """What a document op acts on once its address is settled: ``self`` resolved, or the instance the op created."""

    address: Address = Field(description="The instance the op docks, focuses, closes, or moves")
    title: str | None = Field(description="The title a dock gives the new panel; None when no app lists the address")
    created: Address | None = Field(description="The address the op created through the relay, when it created one")


def _prepare_docking_target(
    shell: ShellState,
    op: str,
    snapshot: LayoutRecord,
    arguments: DocumentOpArguments,
    requester: Address | None,
) -> _DocumentOpTarget:
    """Settle what ``open`` or ``split`` docks, over a snapshot of the arrangement and outside the state lock: the app
    must be registered, a split's anchor must be docked before any create runs (so a bad anchor makes no instance),
    and a bare app with instances is created through the relay here."""
    address = _resolve_op_address(arguments.address, requester)
    entry = shell.inventory.entry(str(address.app))
    if entry is None:
        raise UnknownAppError(f"No registered app named {address.app!r}")
    if address.key is None and entry.row.instances:
        if op == "split":
            _anchor_panel_id(snapshot, arguments.relative_to, requester)
        created = _create_through_relay(shell, entry, arguments)
        return _DocumentOpTarget(address=created.address, title=created.title, created=created.address)
    found = shell.inventory.find_instance(address)
    return _DocumentOpTarget(
        address=address,
        title=found[1].title if found is not None else None,
        created=None,
    )


def _prepare_op_target(
    shell: ShellState,
    op: str,
    snapshot: LayoutRecord,
    arguments: DocumentOpArguments,
    requester: Address | None,
) -> _DocumentOpTarget:
    if is_creating_op(op):
        return _prepare_docking_target(shell, op, snapshot, arguments, requester)
    return _DocumentOpTarget(
        address=_resolve_op_address(arguments.address, requester),
        title=None,
        created=None,
    )


def _dock_target(
    layout: LayoutRecord,
    op: str,
    target: _DocumentOpTarget,
    arguments: DocumentOpArguments,
    requester: Address | None,
) -> LayoutRecord:
    """``open`` or ``split`` over the arrangement as it is at the write: an address it already shows is focused, a
    listed one is docked per the op's placement, and one that is neither is not open anywhere."""
    already_open = panel_id_for_address(layout, target.address)
    if already_open is not None:
        return focus_panel(layout, already_open)
    placement = _docking_placement(layout, op, arguments, requester)
    if target.title is None:
        raise InstanceNotListedError(
            f"No app lists an instance at {target.address}; run `layout.py list` to see every one"
        )
    return add_panel(layout, target.address, mint_tab_id(), target.title, placement)


def _edit_layout_for_op(
    op: str,
    layout: LayoutRecord,
    target: _DocumentOpTarget,
    arguments: DocumentOpArguments,
    requester: Address | None,
) -> LayoutRecord:
    """The arrangement with ``op`` applied. Pure over the layout it is handed, which is the stored one at the moment of
    the write (the panel ids are resolved on it, not on the snapshot the op was prepared over)."""
    if is_creating_op(op):
        return _dock_target(layout, op, target, arguments, requester)
    panel_id = _require_panel(layout, target.address)
    match op:
        case "focus":
            return focus_panel(layout, panel_id)
        case "close":
            return remove_panel(layout, panel_id)
        case "move":
            return move_panel(layout, panel_id, _anchored_placement(layout, arguments, requester))
        case _:
            raise ShellError(f"Op {op!r} has no document handler")


def _op_document(
    shell: ShellState, op: str, args_raw: dict[str, Any], requester: Address | None
) -> ResponseReturnValue:
    """Apply one arrangement op to the target client's layout file and announce the write (contracts.md section 12)."""
    arguments = _parse_document_arguments(args_raw)
    client_id = _require_client(shell, args_raw, requester)
    view_raw, error = _resolve_op_view(shell, args_raw, client_id)
    if error is not None or view_raw is None:
        return error if error is not None else _detail("Failed to resolve the target view", HTTP_INTERNAL_ERROR)
    view_id = ViewId(view_raw)
    if not shell.projects.is_view_known(view_id):
        raise ProjectNotFoundError(view_raw)
    # What the op acts on is settled over a snapshot, outside the state lock (a create may wait on the app for a
    # while); the edit itself runs under the lock over the arrangement as it is then.
    target = _prepare_op_target(
        shell,
        op,
        shell.materialize_client_layout(view_id, client_id),
        arguments,
        requester,
    )
    saved = shell.edit_client_layout(
        view_id,
        client_id,
        lambda layout: _edit_layout_for_op(op, layout, target, arguments, requester),
    )
    if is_creating_op(op) and not is_everything_view(view_id):
        shell.projects.add_tab(view_id, target.address)
        shell.broadcast_projects_updated()
    if op == "close":
        shell.delete_unreferenced_instances()
    if _requested_view(args_raw) is not None and _active_view_of_client(shell, client_id) != str(view_id):
        shell.set_client_active_view(client_id, view_id)
    logger.info(
        "layout op={} requester={} view={} client={} args={}",
        op,
        requester,
        view_id,
        client_id,
        args_raw,
    )
    return jsonify(
        {
            "ok": True,
            "view_id": str(view_id),
            "client_id": str(client_id),
            "layout": layout_inspect(saved, _title_by_address(shell)),
            "created_address": str(target.created) if target.created is not None else None,
        }
    )


def _refuse_unregistered_address(shell: ShellState, args_raw: dict[str, Any]) -> ResponseReturnValue | None:
    """A transient op that names an instance or an app: the address must parse, and an app it names must be registered."""
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


def _is_machine_wide(op: str, args_raw: dict[str, Any]) -> bool:
    """The interface reload, and a refresh of a whole app, reach every window rather than one client's."""
    if op == "reload_system_interface":
        return True
    raw_address = args_raw.get("address")
    return op == "refresh" and isinstance(raw_address, str) and raw_address != SELF_ADDRESS and "?" not in raw_address


def _op_transient(
    shell: ShellState, op: str, args_raw: dict[str, Any], requester: Address | None
) -> ResponseReturnValue:
    """The four verbs with nothing to store: sent to the target client's windows as a ``layout_op`` message."""
    if is_addressed_op(op):
        refusal = _refuse_unregistered_address(shell, args_raw)
        if refusal is not None:
            return refusal
    op_args = _op_only_args(args_raw)
    target_client_id = None if _is_machine_wide(op, args_raw) else str(_require_client(shell, args_raw, requester))
    shell.broadcaster.broadcast_layout_op(
        op,
        op_args,
        requester="" if requester is None else str(requester),
        target_client_id=target_client_id,
    )
    logger.info(
        "layout op={} requester={} target_client={} args={}",
        op,
        requester,
        target_client_id,
        op_args,
    )
    return jsonify({"ok": True, "target_client_id": target_client_id})
