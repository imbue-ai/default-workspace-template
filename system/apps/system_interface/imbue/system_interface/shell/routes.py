"""The shell's HTTP routes beside the desktop routes (desktop contracts.md sections 5 and 8): the client-activity
report, an app's stop and start, the clients and the inventory, and the agent-facing op route."""

import json
from datetime import datetime
from datetime import timezone
from typing import assert_never

from app_manifest.manifest import PinStyle
from app_manifest.primitives import AppName
from flask import Flask
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger

from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.client_activity import summarize_client_activity
from imbue.system_interface.shell.clients import client_wire_json
from imbue.system_interface.shell.clients import entries_wire_json
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import ClientActivityReport
from imbue.system_interface.shell.data_types import EntryPresentation
from imbue.system_interface.shell.desktop_routes import dispatch_desktop_op
from imbue.system_interface.shell.desktop_routes import inventory_document_json
from imbue.system_interface.shell.desktop_routes import register_desktop_routes
from imbue.system_interface.shell.desktop_routes import resolved_client_wire_json
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.errors import DesktopConflictError
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import LastDesktopError
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.errors import NoTargetClientError
from imbue.system_interface.shell.errors import PinnedWindowError
from imbue.system_interface.shell.errors import ShellError
from imbue.system_interface.shell.errors import StalePlacementsSaveError
from imbue.system_interface.shell.errors import SupervisorProgramActionError
from imbue.system_interface.shell.errors import UnknownAppError
from imbue.system_interface.shell.errors import WallpaperNotFoundError
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.layout_ops import CONTEXT_OP
from imbue.system_interface.shell.layout_ops import OpRequester
from imbue.system_interface.shell.layout_ops import is_known_op
from imbue.system_interface.shell.layout_ops import parse_op_requester
from imbue.system_interface.shell.liveness import start_supervisor_program
from imbue.system_interface.shell.liveness import stop_supervisor_program
from imbue.system_interface.shell.liveness import supervisor_socket_path
from imbue.system_interface.shell.primitives import AppLifecycleAction
from imbue.system_interface.shell.primitives import ClientActivityKind
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.route_helpers import HTTP_BAD_GATEWAY
from imbue.system_interface.shell.route_helpers import HTTP_BAD_REQUEST
from imbue.system_interface.shell.route_helpers import HTTP_CONFLICT
from imbue.system_interface.shell.route_helpers import HTTP_INTERNAL_ERROR
from imbue.system_interface.shell.route_helpers import HTTP_NOT_FOUND
from imbue.system_interface.shell.route_helpers import HTTP_NO_CONTENT
from imbue.system_interface.shell.route_helpers import HTTP_PRECONDITION_FAILED
from imbue.system_interface.shell.route_helpers import detail_response
from imbue.system_interface.shell.route_helpers import parse_request_body
from imbue.system_interface.shell.route_helpers import require_loopback
from imbue.system_interface.shell.state import ShellState


def _answer_shell_error(error: ShellError) -> ResponseReturnValue:
    match error:
        case (
            UnknownAppError()
            | ClientNotFoundError()
            | DesktopNotFoundError()
            | WindowNotFoundError()
            | WallpaperNotFoundError()
        ):
            return detail_response(str(error), HTTP_NOT_FOUND)
        case DesktopConflictError() | LastDesktopError() | StalePlacementsSaveError() | PinnedWindowError():
            return detail_response(str(error), HTTP_CONFLICT)
        case InvalidShellValueError() | AppLifecycleRefusedError() | LayoutOpError() | DesktopValueError():
            return detail_response(str(error), HTTP_BAD_REQUEST)
        case NoTargetClientError():
            return detail_response(str(error), HTTP_PRECONDITION_FAILED)
        case _:
            logger.opt(exception=error).error("Failed to serve a shell request")
            return detail_response(str(error), HTTP_INTERNAL_ERROR)


def _shell() -> ShellState:
    return get_state().shell


def _entry_or_raise(name: str) -> AppInventoryEntry:
    entry = _shell().inventory.entry(name)
    if entry is None:
        raise UnknownAppError(f"No registered app named {name!r}")
    return entry


# Section 5.1: the client-activity report


def client_activity_route() -> ResponseReturnValue:
    refusal = require_loopback()
    if refusal is not None:
        return refusal
    report = parse_request_body(ClientActivityReport)
    shell = _shell()
    match report.kind:
        case ClientActivityKind.MESSAGE:
            shell.activity.append_message(
                str(report.client_id), str(report.desktop_id), report.app, report.key, report.text
            )
        case _ as unreachable:
            assert_never(unreachable)
    return "", HTTP_NO_CONTENT


# Section 5: stop and start of an app


def _lifecycle(name: str, action: AppLifecycleAction) -> ResponseReturnValue:
    shell = _shell()
    entry = _entry_or_raise(name)
    program = entry.row.program or ""
    if not program:
        raise AppLifecycleRefusedError(
            f"App {name!r} has no supervised program registered, so it cannot be stopped or started from the workspace"
        )
    # A critical app is never stopped from here, and neither is any row running inside a
    # critical app's program.
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
        return detail_response(str(e), HTTP_BAD_GATEWAY)
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


# Section 5.5: clients and the inventory


def list_clients() -> ResponseReturnValue:
    shell = _shell()
    desktops = shell.list_desktops()
    connected = shell.broadcaster.connected_client_ids()
    return jsonify(
        {
            "clients": [
                resolved_client_wire_json(record, str(record.id) in connected, desktops)
                for record in shell.clients.list_clients()
            ]
        }
    )


def inventory_document() -> ResponseReturnValue:
    return jsonify(inventory_document_json(_shell()))


def set_client_entry(client_id: str, app: str) -> ResponseReturnValue:
    """How one client shows one pinned entry (pinned-taskbar-entries plan section 5.3): the app must be pinned, and
    the style plain or the one its pin declares."""
    body = parse_request_body(EntryPresentation)
    shell = _shell()
    entry = _entry_or_raise(app)
    pin = entry.row.pin
    if pin is None or entry.row.internal:
        raise InvalidShellValueError(f"App {app!r} declares no pinned entry")
    if body.style is not PinStyle.PLAIN and body.style is not pin.style:
        raise InvalidShellValueError(
            f"App {app!r} offers the plain style and {pin.style.value!r}, not {body.style.value!r}"
        )
    record = shell.clients.set_entry_presentation(
        ClientId(client_id), str(AppName(app)), body, datetime.now(timezone.utc)
    )
    shell.broadcaster.broadcast_client_entries_changed(str(record.id), entries_wire_json(record.entries))
    return jsonify(client_wire_json(record, str(record.id) in shell.broadcaster.connected_client_ids()))


# Section 8: the agent-facing op route


def layout_broadcast() -> ResponseReturnValue:
    refusal = require_loopback()
    if refusal is not None:
        return refusal
    try:
        body = json.loads(request.get_data())
    except ValueError as e:
        logger.opt(exception=e).warning("layout broadcast received invalid JSON body")
        return detail_response("Invalid JSON in request body", HTTP_BAD_REQUEST)
    if not isinstance(body, dict):
        return detail_response("Request body must be a JSON object", HTTP_BAD_REQUEST)
    op = body.get("op")
    args_raw = body.get("args", {})
    requester = parse_op_requester(body.get("requester"))
    if not isinstance(op, str) or not is_known_op(op):
        return detail_response(f"Unknown layout op: {op!r}", HTTP_BAD_REQUEST)
    if not isinstance(args_raw, dict):
        return detail_response("``args`` must be a JSON object", HTTP_BAD_REQUEST)
    if op == CONTEXT_OP:
        return _op_context(_shell(), requester)
    return dispatch_desktop_op(_shell(), op, args_raw, requester)


def _op_context(shell: ShellState, requester: OpRequester | None) -> ResponseReturnValue:
    clients = summarize_client_activity(shell.activity.read_events(), shell.broadcaster.get_connected_client_infos())
    logger.info("layout op=context requester={} clients={}", requester, len(clients))
    return jsonify({"ok": True, "clients": clients})


def register_shell_routes(application: Flask) -> None:
    """Register every shell route of desktop contracts.md sections 5, 6, and 8 on ``application``."""
    application.register_error_handler(ShellError, _answer_shell_error)
    register_desktop_routes(application)
    application.add_url_rule(
        "/api/client-activity",
        view_func=client_activity_route,
        methods=["POST"],
        endpoint="client_activity_route",
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
    application.add_url_rule("/api/clients", view_func=list_clients, methods=["GET"], endpoint="list_clients")
    application.add_url_rule(
        "/api/clients/<client_id>/entries/<app>",
        view_func=set_client_entry,
        methods=["POST"],
        endpoint="set_client_entry",
    )
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
