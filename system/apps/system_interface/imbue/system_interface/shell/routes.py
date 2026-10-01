"""The shell's HTTP routes beside the desktop routes (desktop contracts.md sections 5 and 8): the client-activity
report, an app's stop and start, the clients and the inventory, and the agent-facing op route."""

import json
from typing import Any
from typing import Final
from typing import assert_never

from app_manifest.manifest import PinStyle
from app_manifest.primitives import AppName
from flask import Flask
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger
from workspace_layout.answers import ClientsListing
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.ops import ClientActivityReport
from workspace_layout.ops import parse_op_body
from workspace_layout.primitives import ClientActivityKind
from workspace_layout.primitives import ClientId
from workspace_layout.records import EntryPresentation

from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.clients import client_view
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.data_types import ClientShownRequest
from imbue.system_interface.shell.data_types import stoppable_program_of
from imbue.system_interface.shell.desktop_routes import dispatch_desktop_op
from imbue.system_interface.shell.desktop_routes import inventory_document
from imbue.system_interface.shell.desktop_routes import register_desktop_routes
from imbue.system_interface.shell.desktop_routes import resolved_client_view
from imbue.system_interface.shell.embedder_messages import EmbedderMessageRelayRequest
from imbue.system_interface.shell.embedder_messages import deliver_forwarded_message
from imbue.system_interface.shell.embedder_messages import forwarded_messages
from imbue.system_interface.shell.embedder_messages import message_delivery_wire_json
from imbue.system_interface.shell.errors import AppLifecycleRefusedError
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.errors import DesktopConflictError
from imbue.system_interface.shell.errors import DesktopNotFoundError
from imbue.system_interface.shell.errors import DesktopValueError
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import LastDesktopError
from imbue.system_interface.shell.errors import LaunchRefusedError
from imbue.system_interface.shell.errors import LaunchUnavailableError
from imbue.system_interface.shell.errors import LayoutOpError
from imbue.system_interface.shell.errors import NoMessageHandlerError
from imbue.system_interface.shell.errors import NoTargetClientError
from imbue.system_interface.shell.errors import PinnedWindowError
from imbue.system_interface.shell.errors import ShellError
from imbue.system_interface.shell.errors import StalePlacementsSaveError
from imbue.system_interface.shell.errors import SupervisorProgramActionError
from imbue.system_interface.shell.errors import UnknownAppError
from imbue.system_interface.shell.errors import UpdateNoticeCommandError
from imbue.system_interface.shell.errors import UpdateNoticeRefusedError
from imbue.system_interface.shell.errors import WallpaperNotFoundError
from imbue.system_interface.shell.errors import WindowNotFoundError
from imbue.system_interface.shell.port_parking import ParkedPageKind
from imbue.system_interface.shell.primitives import AppLifecycleAction
from imbue.system_interface.shell.route_helpers import HTTP_ACCEPTED
from imbue.system_interface.shell.route_helpers import HTTP_BAD_GATEWAY
from imbue.system_interface.shell.route_helpers import HTTP_BAD_REQUEST
from imbue.system_interface.shell.route_helpers import HTTP_CONFLICT
from imbue.system_interface.shell.route_helpers import HTTP_FORBIDDEN
from imbue.system_interface.shell.route_helpers import HTTP_INTERNAL_ERROR
from imbue.system_interface.shell.route_helpers import HTTP_NOT_FOUND
from imbue.system_interface.shell.route_helpers import HTTP_NO_CONTENT
from imbue.system_interface.shell.route_helpers import HTTP_OK
from imbue.system_interface.shell.route_helpers import HTTP_PRECONDITION_FAILED
from imbue.system_interface.shell.route_helpers import detail_response
from imbue.system_interface.shell.route_helpers import parse_request_body
from imbue.system_interface.shell.route_helpers import require_loopback
from imbue.system_interface.shell.state import ShellState


def _answer_shell_error(error: ShellError) -> ResponseReturnValue:
    match error:
        case (
            UnknownAppError()
            | NoMessageHandlerError()
            | ClientNotFoundError()
            | DesktopNotFoundError()
            | WindowNotFoundError()
            | WallpaperNotFoundError()
        ):
            return detail_response(str(error), HTTP_NOT_FOUND)
        case (
            DesktopConflictError()
            | LastDesktopError()
            | StalePlacementsSaveError()
            | PinnedWindowError()
            | UpdateNoticeRefusedError()
        ):
            return detail_response(str(error), HTTP_CONFLICT)
        case UpdateNoticeCommandError():
            logger.opt(exception=error).error("An update-notice verb failed")
            return detail_response(str(error), HTTP_INTERNAL_ERROR)
        case (
            InvalidShellValueError()
            | AppLifecycleRefusedError()
            | LayoutOpError()
            | DesktopValueError()
            | LaunchRefusedError()
        ):
            return detail_response(str(error), HTTP_BAD_REQUEST)
        case LaunchUnavailableError():
            return detail_response(str(error), HTTP_BAD_GATEWAY)
        case NoTargetClientError():
            return detail_response(str(error), HTTP_PRECONDITION_FAILED)
        case _:
            logger.opt(exception=error).error("Failed to serve a shell request")
            return detail_response(str(error), HTTP_INTERNAL_ERROR)


def _answer_layout_value_error(error: InvalidLayoutValueError) -> ResponseReturnValue:
    """A value off the layout wire's rule (an id, a requester) in a request: the caller's to fix."""
    return detail_response(str(error), HTTP_BAD_REQUEST)


def _shell() -> ShellState:
    return get_state().shell


# What a preview shell answers to a verb whose effect lands outside its own copy of the state: an
# app's stop and start reach supervisord, the update notice's verbs act on the live apply's
# rollback point, and the embedder-message relay posts to live apps. Everything else a preview
# offers (opening, placing, and closing windows, a refresh, the interface reload, the avatar)
# edits the preview's own state or reaches only the preview's own windows, so it stays live for
# the person judging the change.
PREVIEW_REFUSAL_DETAIL: Final[str] = "This is a preview of a proposed change; it cannot change the live workspace."


def _refuse_if_preview() -> ResponseReturnValue | None:
    if get_state().is_preview:
        return detail_response(PREVIEW_REFUSAL_DETAIL, HTTP_FORBIDDEN)
    return None


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
            shell.activity.append_message(report)
        case _ as unreachable:
            assert_never(unreachable)
    return "", HTTP_NO_CONTENT


# Section 5: stop, start, and quit of an app


def _lifecycle(name: str, action: AppLifecycleAction) -> ResponseReturnValue:
    refusal = _refuse_if_preview()
    if refusal is not None:
        return refusal
    shell = _shell()
    entry = _entry_or_raise(name)
    program = _stoppable_program_or_raise(shell, entry)
    try:
        match action:
            case AppLifecycleAction.STOP:
                shell.lifecycle.stop_app(name)
            case AppLifecycleAction.START:
                if shell.lifecycle.wake(name) is ParkedPageKind.FAILED:
                    return detail_response(
                        f"App {name!r} (program {program!r}) could not be started", HTTP_BAD_GATEWAY
                    )
            case _ as unreachable:
                assert_never(unreachable)
    except SupervisorProgramActionError as e:
        return detail_response(str(e), HTTP_BAD_GATEWAY)
    refreshed = shell.inventory.entry(name)
    return jsonify(
        {
            "name": name,
            "is_running": refreshed.is_running if refreshed is not None else False,
        }
    )


def _stoppable_program_or_raise(shell: ShellState, entry: AppInventoryEntry) -> str:
    """The program the workspace may act on for the app; raises AppLifecycleRefusedError (a 400) for an app with no
    supervised program, a critical one, or a row inside a critical app's program."""
    name = str(entry.row.name)
    if not entry.row.program:
        raise AppLifecycleRefusedError(
            f"App {name!r} has no supervised program registered, so it cannot be stopped or started from the workspace"
        )
    program = stoppable_program_of(entry, shell.inventory.entries())
    if program is None:
        raise AppLifecycleRefusedError(
            f"App {name!r} is critical to the workspace and cannot be stopped or started here"
        )
    return program


def stop_app(name: str) -> ResponseReturnValue:
    return _lifecycle(name, AppLifecycleAction.STOP)


def quit_app(name: str) -> ResponseReturnValue:
    """``POST /api/apps/<name>/quit``: close every window of the app, then stop it; refused as a stop is."""
    refusal = _refuse_if_preview()
    if refusal is not None:
        return refusal
    shell = _shell()
    entry = _entry_or_raise(name)
    _stoppable_program_or_raise(shell, entry)
    try:
        shell.quit_app(name)
    except SupervisorProgramActionError as e:
        return detail_response(str(e), HTTP_BAD_GATEWAY)
    refreshed = shell.inventory.entry(name)
    return jsonify({"name": name, "is_running": refreshed.is_running if refreshed is not None else False})


def start_app(name: str) -> ResponseReturnValue:
    return _lifecycle(name, AppLifecycleAction.START)


# Section 5.5: clients and the inventory


def list_clients() -> ResponseReturnValue:
    shell = _shell()
    desktops = shell.list_desktops()
    connected = shell.broadcaster.connected_client_ids()
    listing = ClientsListing(
        clients=tuple(
            resolved_client_view(record, str(record.id) in connected, desktops)
            for record in shell.clients.list_clients()
        )
    )
    return jsonify(listing.model_dump(mode="json"))


def inventory_route() -> ResponseReturnValue:
    return jsonify(inventory_document(_shell()).model_dump(mode="json"))


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
    record = shell.set_client_entry_presentation(ClientId(client_id), AppName(app), body)
    return jsonify(
        client_view(record, str(record.id) in shell.broadcaster.connected_client_ids()).model_dump(mode="json")
    )


def record_client_shown(client_id: str) -> ResponseReturnValue:
    """What a client's phone layout now shows (the phone plan's shown history): a window some desktop holds, or
    ``null`` for its home grid; answers the client record."""
    body = parse_request_body(ClientShownRequest)
    shell = _shell()
    record = shell.record_client_shown(ClientId(client_id), body.window_id)
    return jsonify(
        client_view(record, str(record.id) in shell.broadcaster.connected_client_ids()).model_dump(mode="json")
    )


# Section 5.6: the embedder-message relay


def relay_embedder_message() -> ResponseReturnValue:
    """Post a message the Imbue Studio chrome sent this client's page to every app registered for its type; 200 when
    every app took it, 502 with each app's answer and a ``detail`` naming the ones that did not, 404 when no app
    handles the type. Refused in a preview, whose copied registry names the live app of every sibling not
    previewed."""
    refusal = _refuse_if_preview()
    if refusal is not None:
        return refusal
    relayed = parse_request_body(EmbedderMessageRelayRequest)
    forwarded = forwarded_messages([entry.row for entry in _shell().inventory.entries()], relayed)
    if not forwarded:
        raise NoMessageHandlerError(f"No registered app handles {str(relayed.type)!r}")
    deliveries = [deliver_forwarded_message(message) for message in forwarded]
    logger.info(
        "Relayed {} from client {} to {}",
        relayed.type,
        relayed.client_id,
        ", ".join(f"{delivery.app} ({delivery.status})" for delivery in deliveries),
    )
    answer: dict[str, Any] = {
        "type": str(relayed.type),
        "deliveries": [message_delivery_wire_json(delivery) for delivery in deliveries],
    }
    undelivered = [delivery for delivery in deliveries if not delivery.is_delivered]
    if not undelivered:
        return jsonify(answer), HTTP_OK
    detail = "; ".join(f"{delivery.app} did not take it: {delivery.detail}" for delivery in undelivered)
    return jsonify({**answer, "detail": detail}), HTTP_BAD_GATEWAY


# The update notice: the rollback point the update-app careful flow's apply kept


def pending_update() -> ResponseReturnValue:
    """The kept rollback point of the last careful-flow apply, or ``null`` when there is none."""
    notice = _shell().update_notice.current()
    return jsonify(notice.wire_json() if notice is not None else None)


def confirm_pending_update() -> ResponseReturnValue:
    """ "Everything seems good", or Close on a settled notice: drop the record, and the kept copies with it when no
    rollback ran (a failed rollback's copies stay for an agent). Refused in a preview, which owns no live state."""
    refusal = _refuse_if_preview()
    if refusal is not None:
        return refusal
    _shell().update_notice.confirm()
    return "", HTTP_NO_CONTENT


def rollback_pending_update() -> ResponseReturnValue:
    """ "Roll back": start the rollback detached and answer once it is under way, with its first progress in the
    record; the rest of its progress and its outcome follow on the socket."""
    refusal = _refuse_if_preview()
    if refusal is not None:
        return refusal
    _shell().update_notice.launch_rollback()
    return detail_response("The rollback has started.", HTTP_ACCEPTED)


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
    parsed = parse_op_body(body)
    return dispatch_desktop_op(_shell(), parsed.op, parsed.args, parsed.requester)


def register_shell_routes(application: Flask) -> None:
    """Register every shell route of desktop contracts.md sections 5, 6, and 8 on ``application``."""
    application.register_error_handler(ShellError, _answer_shell_error)
    application.register_error_handler(InvalidLayoutValueError, _answer_layout_value_error)
    register_desktop_routes(application)
    application.add_url_rule(
        "/api/client-activity",
        view_func=client_activity_route,
        methods=["POST"],
        endpoint="client_activity_route",
    )
    application.add_url_rule(
        "/api/updates/pending",
        view_func=pending_update,
        methods=["GET"],
        endpoint="pending_update",
    )
    application.add_url_rule(
        "/api/updates/pending/confirm",
        view_func=confirm_pending_update,
        methods=["POST"],
        endpoint="confirm_pending_update",
    )
    application.add_url_rule(
        "/api/updates/pending/rollback",
        view_func=rollback_pending_update,
        methods=["POST"],
        endpoint="rollback_pending_update",
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
        "/api/apps/<name>/quit",
        view_func=quit_app,
        methods=["POST"],
        endpoint="quit_app",
    )
    application.add_url_rule("/api/clients", view_func=list_clients, methods=["GET"], endpoint="list_clients")
    application.add_url_rule(
        "/api/clients/<client_id>/entries/<app>",
        view_func=set_client_entry,
        methods=["POST"],
        endpoint="set_client_entry",
    )
    application.add_url_rule(
        "/api/clients/<client_id>/shown",
        view_func=record_client_shown,
        methods=["POST"],
        endpoint="record_client_shown",
    )
    application.add_url_rule(
        "/api/inventory",
        view_func=inventory_route,
        methods=["GET"],
        endpoint="inventory_document",
    )
    application.add_url_rule(
        "/api/embedder-messages",
        view_func=relay_embedder_message,
        methods=["POST"],
        endpoint="relay_embedder_message",
    )
    application.add_url_rule(
        "/api/layout/broadcast",
        view_func=layout_broadcast,
        methods=["POST"],
        endpoint="layout_broadcast",
    )
