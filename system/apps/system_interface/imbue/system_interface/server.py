import json
import queue
import re
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from app_manifest.built_assets import send_built_asset
from app_manifest.content_security_policy import attach_document_policy
from app_manifest.manifest import describe_validation_error
from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import CONTEXT_MENU_ROUTE
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry import SHELL_CONTEXT_MENU_PATH
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import register_api_schema_route
from app_schema.errors import InvalidRequestBodyError
from app_schema.request_guard import install_request_guard
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from flask import redirect
from flask import request
from flask import send_file
from flask.typing import ResponseReturnValue
from loguru import logger as _loguru_logger
from pydantic import ValidationError
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import NotFound
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.identity import participant_key_of
from workspace_layout.primitives import ParticipantKind
from workspace_layout.primitives import UserId

from imbue.system_interface.api_models import HeartbeatRequest
from imbue.system_interface.api_models import HeartbeatResponse
from imbue.system_interface.api_models import PresenceListing
from imbue.system_interface.api_models import PresentUserView
from imbue.system_interface.api_models import ShellHealthResponse
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import attach_state
from imbue.system_interface.app_context import get_state
from imbue.system_interface.avatar.icons import DEFAULT_ICON_SIZE
from imbue.system_interface.avatar.routes import icon_png_response
from imbue.system_interface.avatar.routes import register_avatar_routes
from imbue.system_interface.avatar.routes import selected_design_id
from imbue.system_interface.avatar.status import avatar_status_wire_json
from imbue.system_interface.documents import MANIFEST_PATH
from imbue.system_interface.documents import SHELL_BACKGROUND_COLOR
from imbue.system_interface.documents import THEME_GALLERY_ROUTE
from imbue.system_interface.documents import TOUCH_ICON_PATH
from imbue.system_interface.documents import document_response
from imbue.system_interface.documents import inject_base_path_meta_tag
from imbue.system_interface.documents import inject_install_tags
from imbue.system_interface.documents import inject_meta_tag
from imbue.system_interface.presence import PresenceOutcome
from imbue.system_interface.presence import PresentUser
from imbue.system_interface.presence import present_users_wire_json
from imbue.system_interface.presence import reported_input_moment
from imbue.system_interface.presence import utc_now
from imbue.system_interface.recovery_page import DESKTOPS_RECOVERY_PATH
from imbue.system_interface.recovery_page import DesktopsLoadFailure
from imbue.system_interface.recovery_page import INTERFACE_NOT_BUILT_PAGE
from imbue.system_interface.recovery_page import INTERFACE_NOT_BUILT_REPAIR_MESSAGE
from imbue.system_interface.recovery_page import RECOVERY_LAUNCH_PATH
from imbue.system_interface.recovery_page import desktops_not_loaded_page
from imbue.system_interface.recovery_page import desktops_repair_message
from imbue.system_interface.recovery_page import render_recovery_page
from imbue.system_interface.request_helpers import handle_unhandled_exception
from imbue.system_interface.route_table import PRESENCE_HEARTBEAT_PATH
from imbue.system_interface.route_table import PRESENCE_PATH
from imbue.system_interface.route_table import ROSTER_ROUTE
from imbue.system_interface.route_table import SHELL_ROUTE_TABLE
from imbue.system_interface.shell.data_types import ClientStateReport
from imbue.system_interface.shell.data_types import PopOutStateReport
from imbue.system_interface.shell.desktop_routes import inventory_document
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import RefusalKind
from imbue.system_interface.shell.errors import ShellError
from imbue.system_interface.shell.errors import ShellStateError
from imbue.system_interface.shell.keybindings_routes import register_keybindings_routes
from imbue.system_interface.shell.recovery_launches import RecoveryLaunchAnswer
from imbue.system_interface.shell.recovery_launches import RecoveryLaunchPurpose
from imbue.system_interface.shell.recovery_launches import RecoveryLaunchRequest
from imbue.system_interface.shell.recovery_launches import RecoveryReason
from imbue.system_interface.shell.recovery_launches import TERMINAL_APP_NAME
from imbue.system_interface.shell.recovery_launches import repair_agent_launch
from imbue.system_interface.shell.recovery_launches import terminal_launch
from imbue.system_interface.shell.route_helpers import HTTP_CONFLICT
from imbue.system_interface.shell.route_helpers import HTTP_FORBIDDEN
from imbue.system_interface.shell.route_helpers import HTTP_NOT_FOUND
from imbue.system_interface.shell.route_helpers import IMAGE_CONTENT_SECURITY_POLICY
from imbue.system_interface.shell.route_helpers import detail_response
from imbue.system_interface.shell.route_helpers import request_identity
from imbue.system_interface.shell.routes import PREVIEW_REFUSAL_DETAIL
from imbue.system_interface.shell.routes import register_shell_routes
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.tour import ONBOARDING_MEDIA_ORIGIN
from imbue.system_interface.update_staleness import PREVIEW_META_CONTENT
from imbue.system_interface.update_staleness import PREVIEW_META_TAG
from imbue.system_interface.update_staleness import UPDATE_STALENESS_META_TAG
from imbue.system_interface.wsgi import build_sock

# An app's origin label as ``forward_port.py`` mints it: one DNS label. Read back rather than
# assumed, because the registry is a file on disk that other things write; a row that could
# not be a hostname could not name an origin either.
_ORIGIN_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

logger = _loguru_logger

_DESKTOPS_LOAD_NOW_SENTENCE: Final[str] = "This workspace's desktops load now."


def _shell() -> ShellState:
    return get_state().shell


def _terminal_origin_label() -> str | None:
    """The terminal app's unguessable origin label, or None when there is no terminal to offer."""
    entry = _shell().inventory.entry(TERMINAL_APP_NAME)
    if entry is None or not _ORIGIN_LABEL_PATTERN.match(entry.row.label):
        return None
    return entry.row.label


def _inject_update_staleness_meta_tag(html_content: str, staleness: str | None) -> str:
    """Inject the update-staleness variant so the frontend can render its banner.

    Injected only when stale: the banner keys off the tag's presence, and a
    consistent workspace's shell carries no tag at all.
    """
    if staleness is None:
        return html_content
    return inject_meta_tag(html_content, UPDATE_STALENESS_META_TAG, staleness)


def _shell_update_staleness() -> str | None:
    """The staleness variant to inject into this app shell, if any.

    Asked per built-shell request, so a tree that moved -- or an apply marker
    that appeared -- after this process started is still seen. Skipped for
    ``HEAD``: that is the not-built placeholder's own poll, once every ten
    seconds per open tab for the length of an outage, and the placeholder
    itself never asks (it carries no banner). Reading staleness forks git, and
    an outage is precisely when the tree has moved and both of its reads run.
    Skipped for a preview shell too: it serves a worktree the live tree is
    expected to differ from, so the banner would only ever say so.
    """
    if request.method == "HEAD" or get_state().is_preview:
        return None
    return get_state().update_staleness.staleness()


def _inject_preview_meta_tag(html_content: str, is_preview: bool) -> str:
    """Mark a preview shell's page so the frontend hides the verbs the backend refuses."""
    if not is_preview:
        return html_content
    return inject_meta_tag(html_content, PREVIEW_META_TAG, PREVIEW_META_CONTENT)


def _root_path() -> str:
    """The path prefix the shell is served under, with no trailing slash (empty at the origin's root)."""
    return (request.script_root or "").rstrip("/")


def _index() -> Response:
    """The workspace's page: the desktop its windows render in, or a placeholder while the frontend is not built."""
    state = get_state()
    index_path = state.static_directory / "index.html"
    if index_path.exists():
        staleness = _shell_update_staleness()
        root_path = _root_path()
        html_content = index_path.read_text()
        html_content = inject_base_path_meta_tag(html_content, root_path)
        html_content = inject_install_tags(html_content, state.workspace_name.resolve(), root_path)
        html_content = _inject_update_staleness_meta_tag(html_content, staleness)
        html_content = _inject_preview_meta_tag(html_content, state.is_preview)
        return document_response(html_content, is_frontend_built=True)
    return _frontend_not_built_response(index_path)


def _frontend_not_built_response(missing_page: Path) -> Response:
    """Render the recovery page shown when there is no compiled bundle to serve; ``missing_page`` is the built page
    the request wanted.

    A ``HEAD`` gets the header and nothing else. That is the page's own
    poll asking whether the bundle is back yet -- every ten seconds, for every
    open tab, for as long as the outage lasts -- and answering it in full would
    re-read the app registry and re-render the page each time, and bury the one
    diagnostic below in six repetitions a minute of itself. Werkzeug drops the
    body of a HEAD response anyway, so the caller sees no difference.
    """
    if request.method == "HEAD":
        return document_response("", is_frontend_built=False)
    # Logged with the resolved directory because the usual cause is that the
    # served tree was replaced under a running service, which is otherwise
    # invisible from the supervisor logs.
    _loguru_logger.warning("Served the recovery page: no frontend bundle at {}", missing_page)
    is_agent_offered = (
        repair_agent_launch(_shell().inventory.entries(), INTERFACE_NOT_BUILT_REPAIR_MESSAGE) is not None
    )
    page = render_recovery_page(INTERFACE_NOT_BUILT_PAGE, _terminal_origin_label(), is_agent_offered, _root_path())
    return document_response(page, is_frontend_built=False)


def _read_desktops_failure() -> DesktopsLoadFailure | None:
    """Run the read ``GET /api/inventory`` answers, and say why it failed; None when it succeeds."""
    try:
        inventory_document(_shell())
    except ShellError as e:
        _loguru_logger.opt(exception=e).warning("Failed to read the desktops and apps for the recovery page")
        return DesktopsLoadFailure(details=str(e), is_out_of_space=e.kind == RefusalKind.OUT_OF_SPACE)
    # Whatever else the read raises is what the inventory route answered with its 500, and this page exists to show it
    # and hand it to an agent; it is logged with its traceback, as the shell's unhandled-exception handler logs it.
    except Exception as e:
        _loguru_logger.opt(exception=e).error("Failed to read the desktops and apps for the recovery page")
        return DesktopsLoadFailure(details="".join(traceback.format_exception(e)).rstrip(), is_out_of_space=False)
    return None


def _desktop_with_query() -> str:
    """The shell's root, with this request's query (a pulled-out window's own page keeps its own)."""
    query = request.query_string.decode()
    return f"{_root_path()}/" + (f"?{query}" if query else "")


def _desktops_recovery_page() -> ResponseReturnValue:
    """``GET /recovery/desktops``: where the frontend sends its page when the shell answered the read of the desktops
    and apps with an error. The page reads them again itself, so what it says is the shell's own failure rather than
    anything a URL could carry, and sends the page back to the desktop at once when the read succeeds."""
    failure = _read_desktops_failure()
    if failure is None:
        return redirect(_desktop_with_query())
    is_agent_offered = repair_agent_launch(_shell().inventory.entries(), desktops_repair_message(failure)) is not None
    page = render_recovery_page(
        desktops_not_loaded_page(failure), _terminal_origin_label(), is_agent_offered, _root_path()
    )
    return document_response(page, is_frontend_built=False)


def _recovery_repair_message(reason: RecoveryReason) -> str | None:
    """What the repair agent is told about ``reason``; None when there is nothing left to repair (the desktops load)."""
    match reason:
        case RecoveryReason.INTERFACE_NOT_BUILT:
            return INTERFACE_NOT_BUILT_REPAIR_MESSAGE
        case RecoveryReason.DESKTOPS_NOT_LOADED:
            failure = _read_desktops_failure()
            return None if failure is None else desktops_repair_message(failure)
        case _ as unreachable:
            assert_never(unreachable)


def _recovery_launch() -> ResponseReturnValue:
    """``POST /api/recovery/launch``: run one of the recovery page's launches -- a terminal session, or the repair
    agent asked through the primary text action about what the page is about -- and answer where its page is: the
    app's origin label, the path, and the app's display name. 404 when no app offers it; 409 when the desktops the
    agent would be asked about load now; a launch's own failures answer as the launch route's do. A preview refuses,
    since either would act on the live workspace."""
    if get_state().is_preview:
        return detail_response(PREVIEW_REFUSAL_DETAIL, HTTP_FORBIDDEN, user_facing_message=PREVIEW_REFUSAL_DETAIL)
    launch_request = parse_request_body(RecoveryLaunchRequest)
    shell = _shell()
    entries = shell.inventory.entries()
    match launch_request.purpose:
        case RecoveryLaunchPurpose.TERMINAL:
            found = terminal_launch(entries)
        case RecoveryLaunchPurpose.REPAIR_AGENT:
            message = _recovery_repair_message(launch_request.reason)
            if message is None:
                return detail_response(
                    "the desktops and apps load now, so there is nothing to ask an agent about",
                    HTTP_CONFLICT,
                    user_facing_message=_DESKTOPS_LOAD_NOW_SENTENCE,
                )
            found = repair_agent_launch(entries, message)
        case _ as unreachable:
            assert_never(unreachable)
    if found is None:
        return detail_response(
            f"No app in this workspace offers a {launch_request.purpose.value} launch", HTTP_NOT_FOUND
        )
    path = shell.launch_destination(found.entry, found.launch_path, found.params, None, None, None)
    return model_response(
        RecoveryLaunchAnswer(label=str(found.entry.row.label), path=str(path), display_name=found.entry.display_name)
    )


def _theme_gallery() -> Response:
    """The theme gallery page (workspace-themes plan section 8.2), a second page of the shell's build."""
    gallery_path = get_state().static_directory / THEME_GALLERY_FILENAME
    if not gallery_path.exists():
        return _frontend_not_built_response(gallery_path)
    html_content = inject_base_path_meta_tag(gallery_path.read_text(), _root_path())
    return document_response(html_content, is_frontend_built=True)


def _index_catch_all(path: str) -> Response:
    """The workspace's page again, for every path the page routes on its own; an unknown API path is a 404."""
    # Every other path is a client-side route and renders the app shell -- except an
    # unknown API path, whose caller wants an answer it can parse, not a page.
    if path == API_PREFIX.strip("/") or path.startswith(API_PREFIX):
        return error_response(f"No such API route: /{path}", HTTP_NOT_FOUND)
    return _index()


def _health_endpoint() -> Response:
    """The probe route of contracts.md section 5: alive, and whether the built frontend is being served."""
    is_frontend_built = (get_state().static_directory / "index.html").exists()
    return model_response(ShellHealthResponse(status="ok", is_frontend_built=is_frontend_built))


THEME_GALLERY_FILENAME: Final[str] = "theme-gallery.html"

# Every route the shell answers as JSON lives under it; an unknown path under it is a JSON 404.
API_PREFIX: Final[str] = "api/"


def _present_users_wire_json(users: Sequence[PresentUser]) -> list[dict[str, Any]]:
    return present_users_wire_json(users, get_state().profiles, utc_now())


def _presence_endpoint() -> Response:
    """The connected users right now (one entry per user), for backends and tests that prefer HTTP to the files."""
    users = get_state().presence.connected_users(utc_now())
    return model_response(
        PresenceListing(users=tuple(PresentUserView.model_validate(user) for user in _present_users_wire_json(users)))
    )


def _roster_wire_json() -> dict[str, Any]:
    return get_state().roster.document(utc_now()).model_dump(mode="json")


def _broadcast_presence_if_changed(outcome: PresenceOutcome) -> None:
    """Announce what a heartbeat or a sweep changed: the connected set and the roster when someone came or went,
    the roster alone when a person's newest input time moved (the multiplayer spec, section 4.3), since every other
    window reads "Active" or "Idle" off the roster it was last pushed."""
    if outcome.is_membership_changed:
        _shell().broadcaster.broadcast_presence_updated(_present_users_wire_json(outcome.users))
    if outcome.is_membership_changed or outcome.is_input_changed:
        get_state().roster_announcer.announce()


def _roster_endpoint() -> Response:
    """Everyone who may be here, in roster order (the multiplayer spec, section 4.2)."""
    return model_response(get_state().roster.document(utc_now()))


# A picture a page's cache may keep for good: its URL carries the version, so a newer picture is a newer URL. Only
# an answer under the current version is kept so, since a stale page asking under an older one gets these bytes too.
_VERSIONED_PICTURE_CACHE_CONTROL: Final[str] = "public, max-age=31536000, immutable"
# The participant keys that name a person, whose id part the profile is kept under.
_USER_KEY_PREFIX: Final[str] = "user:"


def _participant_picture_endpoint(key: str) -> ResponseReturnValue:
    """A person's profile picture from the shell's own origin (the multiplayer spec, section 4.2): fetched from the
    profile's URL once and kept, so the pages, whose policy admits no outside image, can show it. 404 for a key that
    is not a person's the roster lists, or a person with no picture."""
    now = utc_now()
    roster = get_state().roster
    # Only a person the roster lists by account has a picture here: no id a page names is parsed, fetched, or
    # cached unless the roster built the key from a valid one.
    is_listed_person = key.startswith(_USER_KEY_PREFIX) and any(
        str(entry.key) == key and entry.kind == ParticipantKind.PERSON for entry in roster.document(now).participants
    )
    if not is_listed_person:
        return error_response("Only a person listed in the roster has a profile picture here", HTTP_NOT_FOUND)
    picture = roster.profiles.resolve_picture(UserId(key.removeprefix(_USER_KEY_PREFIX)), now)
    if picture is None:
        return error_response("No profile picture", HTTP_NOT_FOUND)
    response = Response(picture.content, mimetype=picture.media_type)
    # The bytes are an outside host's, under whatever image type it declared: sent as the shell's own images are.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = IMAGE_CONTENT_SECURITY_POLICY
    is_current_version = request.args.get("v") == picture.version
    response.headers["Cache-Control"] = _VERSIONED_PICTURE_CACHE_CONTROL if is_current_version else "no-cache"
    return response


def _parse_heartbeat() -> HeartbeatRequest:
    """The heartbeat's body, read here rather than by ``parse_request_body`` because a page from before the body was
    sent posts none at all, which is a heartbeat carrying nothing; anything else is refused as the helper refuses it."""
    if not request.get_data():
        return HeartbeatRequest()
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise InvalidRequestBodyError("the request body must be a JSON object")
    try:
        return HeartbeatRequest.model_validate(body)
    except ValidationError as e:
        raise InvalidRequestBodyError(describe_validation_error(e)) from e


def _presence_heartbeat_endpoint() -> Response:
    """One shell page's heartbeat: record the requester's presence (the owner of an unshared workspace under the
    ``owner`` key), the page's newest input time, and the windows a person interacted with since the last heartbeat
    (the app-usage-signals spec, section 6.3), and answer their own identity record and participant key.

    Answers 204 and records nothing for a visiting agent, which never heartbeats: there is no file for one here.
    """
    identity = request_identity()
    heartbeat = _parse_heartbeat()
    if identity.agent_id is not None:
        return Response(status=204)
    now = utc_now()
    try:
        outcome = get_state().presence.heartbeat(
            identity, now, reported_input_moment(now, heartbeat.input_age_seconds, heartbeat.last_input_at)
        )
    except (InvalidShellValueError, InvalidLayoutValueError) as e:
        return error_response(str(e), 400)
    _broadcast_presence_if_changed(outcome)
    if heartbeat.client_id is not None:
        _shell().record_window_input(heartbeat.client_id, heartbeat.input_windows)
    return model_response(HeartbeatResponse(identity=identity, participant=participant_key_of(identity)))


# The icon sizes the manifest offers: the touch icon's, and the two an installing browser asks a manifest for.
_MANIFEST_ICON_SIZES: Final[tuple[int, ...]] = (DEFAULT_ICON_SIZE, 192, 512)
_MANIFEST_MIMETYPE: Final[str] = "application/manifest+json"


def _touch_icon() -> ResponseReturnValue:
    """The selected avatar as the home-screen icon iOS asks the page for."""
    return icon_png_response(selected_design_id(_shell()), DEFAULT_ICON_SIZE)


def _web_manifest() -> Response:
    """The web app manifest: the workspace's name, the shell's root as where it opens and what it covers, standalone,
    the page colour, and the selected avatar's icons."""
    root = f"{_root_path()}/"
    name = get_state().workspace_name.resolve()
    design_id = selected_design_id(_shell())
    manifest = {
        "name": name,
        "short_name": name,
        "start_url": root,
        "scope": root,
        "display": "standalone",
        "background_color": SHELL_BACKGROUND_COLOR,
        "theme_color": SHELL_BACKGROUND_COLOR,
        "icons": [
            {
                "src": f"{root}api/avatars/{design_id}/icon.png?size={size}",
                "sizes": f"{size}x{size}",
                "type": "image/png",
            }
            for size in _MANIFEST_ICON_SIZES
        ],
    }
    response = Response(json.dumps(manifest), mimetype=_MANIFEST_MIMETYPE)
    response.headers["Cache-Control"] = "no-cache"
    return response


def _serve_shell_module(basename: str) -> Response:
    """Serve one of the browser-side modules every app serves (the contract, desktop contracts.md section 7, and
    the element context menu) from the shell's own origin.

    An app page imports them from its own origin (each app serves the same build output), since a
    cross-origin module import carries no cookie and the forwarder refuses it; this copy is what
    the e2e stub pages import.
    """
    module_path = get_state().static_directory / "_static" / basename
    if not module_path.is_file():
        return Response(status=404)
    response = send_file(module_path, mimetype="text/javascript")
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def _serve_app_contract() -> Response:
    """The app contract module: how an app's page talks to the shell framing it."""
    return _serve_shell_module(SHELL_APP_CONTRACT_PATH.name)


def _serve_context_menu() -> Response:
    """The element context menu module: the right-click menu that hands an element on a page to a chat."""
    return _serve_shell_module(SHELL_CONTEXT_MENU_PATH.name)


def _favicon() -> Response:
    """The page's icon."""
    favicon_path = get_state().static_directory / "favicon.ico"
    if favicon_path.exists():
        return send_file(favicon_path, mimetype="image/x-icon")
    return Response(status=404)


def _serve_asset(filename: str) -> Response:
    """One file of the page's built bundle, by name; one named from its content is cached for good, so a load (a
    phone opening the page over a slow link above all) does not ask again for files that cannot have changed."""
    assets_directory = get_state().static_directory / "assets"
    # A missing asset is a plain 404, as for the favicon above, rather than the
    # HTML error page ``send_from_directory`` would raise. Existence and safety
    # are both left to ``send_from_directory``: ``filename`` arrives with any
    # ``..`` segments intact, so joining it onto the directory ourselves would
    # stat paths outside it -- an existence oracle for the whole filesystem.
    try:
        return send_built_asset(assets_directory, filename)
    except NotFound:
        return Response(status=404)


def _ws_endpoint(websocket: Any) -> None:
    """The one WebSocket per window (desktop contracts.md section 6)."""
    state = get_state()
    _run_ws_broadcast_loop(
        websocket=websocket,
        shell=state.shell,
        initial_presence=_present_users_wire_json(state.presence.connected_users(utc_now())),
        initial_roster=_roster_wire_json(),
    )


def _handle_client_state_message(
    raw_message: str,
    client_queue: "queue.Queue[str | None]",
    shell: ShellState,
    is_first_report: bool,
) -> bool:
    """Process one incoming WebSocket message; returns True for a well-formed ``client_state``.

    A ``ping`` is answered with a ``pong`` on the same connection: a page coming back into sight asks whether its
    socket survived the time away, since a connection the network dropped can still read as open (the shell
    frontend's ``ShellSocket.reviveNow``).

    ``client_state`` is the only other message type clients send: it registers the browser's client id and the
    desktop it is on, on connect and on every switch. Registration feeds the broadcaster's client registry
    (which targets layout ops), the client record, and the client-activity log (a ``desktop_switch`` when
    the report names a different previous desktop). A following report (a window that followed the client's stored
    desktop, pushed or read on a reconnect) registers the connection and only stamps the client as seen. A moving
    report made before a move its page had not heard of is not recorded, and its connection is told the record. A
    pop-out's report registers its connection under its client and nothing more: the client's record and active
    desktop are its main window's.
    """
    try:
        parsed = json.loads(raw_message)
    except json.JSONDecodeError as e:
        _loguru_logger.opt(exception=e).warning("Ignored unparsable WebSocket message from client")
        return False
    if isinstance(parsed, dict) and parsed.get("type") == "ping":
        client_queue.put(json.dumps({"type": "pong"}))
        return False
    if not isinstance(parsed, dict) or parsed.get("type") != "client_state":
        _loguru_logger.warning("Ignored unexpected WebSocket message type from client: {!r}", parsed)
        return False
    body = {key: value for key, value in parsed.items() if key != "type"}
    if body.get("is_pop_out") is True:
        return _register_pop_out(body, client_queue, shell, is_first_report)
    try:
        report = ClientStateReport.model_validate(body)
    except ValidationError as e:
        _loguru_logger.warning("Ignored a malformed client_state report: {}", e.errors()[0]["msg"])
        return False
    shell.broadcaster.set_client_info(client_queue, str(report.client_id), str(report.active_desktop))
    # A window that followed the stored desktop may name a desktop a later move has already replaced: recording it
    # would move the client back and set its windows following again. So it only stamps the client as seen.
    # A state file the shell cannot write is a warning, not a dropped socket: the live
    # registration above is what the layout ops need, and the next report retries the write.
    is_superseded = False
    if report.is_following:
        try:
            shell.record_client_seen(report.client_id)
        except ClientNotFoundError as e:
            _loguru_logger.opt(exception=e).debug("No client record to stamp as seen for {}", report.client_id)
        except ShellStateError as e:
            _loguru_logger.opt(exception=e).warning("Could not stamp the client {} as seen", report.client_id)
    else:
        is_superseded = _record_moving_report(report, client_queue, shell)
    if is_first_report:
        _loguru_logger.info(
            "WS client registered: client_id={} desktop={} (conn {})",
            report.client_id,
            report.active_desktop,
            id(client_queue),
        )
        return True
    if not is_superseded:
        _log_client_switches(report, client_queue, shell)
    return True


def _record_moving_report(
    report: ClientStateReport, client_queue: "queue.Queue[str | None]", shell: ShellState
) -> bool:
    """Record a report that moves the client; answers whether it was superseded, made before a move its page had not
    heard of. A superseded report is not recorded: the page follows that move instead, and since the move may have been
    announced before this connection could hear it (an op landing as the page loads), the connection is told the
    client's record again; a window that heard it already ignores it."""
    try:
        outcome = shell.record_client_report(report)
    except ShellStateError as e:
        _loguru_logger.opt(exception=e).warning("Could not record the client report for {}", report.client_id)
        return False
    if not outcome.is_superseded:
        return False
    _loguru_logger.info(
        "WS client {} reported desktop {} at revision {}, before a later move: not recorded (conn {})",
        report.client_id,
        report.active_desktop,
        report.revision,
        id(client_queue),
    )
    shell.broadcaster.send_active_desktop_changed_to(
        client_queue, str(report.client_id), str(outcome.record.active_desktop), outcome.record.desktop_revision
    )
    return True


def _register_pop_out(
    body: dict[str, Any],
    client_queue: "queue.Queue[str | None]",
    shell: ShellState,
    is_first_report: bool,
) -> bool:
    try:
        report = PopOutStateReport.model_validate(body)
    except ValidationError as e:
        _loguru_logger.warning("Ignored a malformed pop-out client_state report: {}", e.errors()[0]["msg"])
        return False
    shell.broadcaster.set_pop_out_info(client_queue, str(report.client_id))
    if is_first_report:
        _loguru_logger.info("WS pop-out registered: client_id={} (conn {})", report.client_id, id(client_queue))
    return True


def _log_client_switches(
    report: ClientStateReport, client_queue: "queue.Queue[str | None]", shell: ShellState
) -> None:
    """Log, and append to the activity log, the desktop switch a re-report names (a following report, and a report
    whose previous desktop is empty or unchanged, names none)."""
    is_desktop_switch = (
        not report.is_following and bool(report.previous_desktop) and report.previous_desktop != report.active_desktop
    )
    # A switch the log cannot take is a warning: the record already moved the client.
    if is_desktop_switch:
        _loguru_logger.info(
            "WS client {} switched desktop {} -> {} (conn {})",
            report.client_id,
            report.previous_desktop,
            report.active_desktop,
            id(client_queue),
        )
        try:
            shell.activity.append_desktop_switch(
                str(report.client_id), report.previous_desktop, str(report.active_desktop)
            )
        except OSError as e:
            _loguru_logger.opt(exception=e).warning("Could not log the desktop switch for {}", report.client_id)


def _run_ws_broadcast_loop(
    websocket: Any, shell: ShellState, initial_presence: list[dict[str, Any]], initial_roster: dict[str, Any]
) -> None:
    """Stream the shell broadcaster's messages to ``websocket`` until the client disconnects.

    Each WebSocket connection owns its own thread (flask-sock + the threaded WSGI server), so
    this loop blocks on the per-client queue and forwards messages. flask-sock's keepalive
    closes a half-dead peer, surfacing as ``ConnectionClosed`` from ``send``; the broadcaster
    can also evict a hopelessly-behind client by pushing the shutdown sentinel (``None``).

    The connect sends the inventory, the desktops, the connected users, and the roster
    (``initial_presence`` and ``initial_roster``, read by the caller before the loop starts) so a window paints
    from one snapshot; later changes arrive as broadcasts.

    Incoming ``client_state`` registrations are drained non-blockingly on each loop iteration.
    """
    ws_broadcaster = shell.broadcaster
    client_queue = ws_broadcaster.register()
    _loguru_logger.info("WS /api/ws connection opened (conn {})", id(client_queue))
    disconnect_reason = "handler exited"
    try:
        websocket.send(
            json.dumps(
                {"type": "apps_updated", "apps": [app.model_dump(mode="json") for app in shell.inventory.views()]}
            )
        )
        websocket.send(
            json.dumps(
                {
                    "type": "desktops_updated",
                    "desktops": [
                        desktop.model_dump(mode="json") for desktop in shell.desktop_views(shell.list_desktops())
                    ],
                }
            )
        )
        websocket.send(json.dumps({"type": "avatar_status", **avatar_status_wire_json(shell.avatar_status.current())}))
        # The notice too, so a window that reconnects after a rollback restarted this shell
        # sees the outcome without a fetch of its own.
        notice = shell.update_notice.current()
        websocket.send(
            json.dumps({"type": "update_notice_changed", "notice": notice.wire_json() if notice is not None else None})
        )
        websocket.send(json.dumps({"type": "presence_updated", "users": initial_presence}))
        websocket.send(json.dumps({"type": "roster_updated", **initial_roster}))
        # The chats' activity too, when the chat app has reported it: the agent's card lists it from the connect.
        chats_activity = shell.chats_activity.current()
        if chats_activity is not None:
            websocket.send(json.dumps({"type": "chats_activity", **chats_activity.model_dump(mode="json")}))
        # The tray's resource gauge as it stands; later changes are broadcast by its monitor.
        websocket.send(
            json.dumps({"type": "resource_gauge_changed", "view": shell.resource_gauge.view().model_dump(mode="json")})
        )

        is_client_registered = False
        shutdown = False
        while not shutdown:
            incoming = websocket.receive(timeout=0)
            while incoming is not None:
                if (
                    _handle_client_state_message(
                        str(incoming), client_queue, shell, is_first_report=not is_client_registered
                    )
                    and not is_client_registered
                ):
                    is_client_registered = True
                    # A window registering changes which clients are connected (a socket that dropped and came
                    # back among them), which the tray's viewing ring reads off the clients listing.
                    ws_broadcaster.broadcast_clients_updated()
                incoming = websocket.receive(timeout=0)
            try:
                message = client_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                shutdown = True
                disconnect_reason = "shutdown sentinel (evicted by broadcaster or server shutdown)"
            else:
                websocket.send(message)
    except ConnectionClosed:
        disconnect_reason = "connection closed"
    finally:
        client_info = ws_broadcaster.get_client_info(client_queue)
        _loguru_logger.info(
            "WS /api/ws connection closed (conn {}, client_id={}, reason: {})",
            id(client_queue),
            client_info.client_id if client_info is not None else "<unregistered>",
            disconnect_reason,
        )
        ws_broadcaster.unregister(client_queue)
        # A registered window going away changes which clients are connected and where, which the tray's
        # viewing ring reads off the clients listing; a window that never registered was never listed.
        if client_info is not None:
            ws_broadcaster.broadcast_clients_updated()


APP_DISPLAY_NAME: Final[str] = "Workspace"
APP_DESCRIPTION: Final[str] = "The workspace's shell: its windows, desktops and apps, and who is here."


def create_application(state: SystemInterfaceState) -> Flask:
    """Assemble the Flask app around an already-built ``SystemInterfaceState``.

    Pure assembler: it wires routes and error handling onto the app and attaches the injected
    ``state``. It constructs no collaborators and starts nothing. The composition root
    (``main.build_production_state`` plus ``main.main``) builds the real object graph and
    starts the shell; tests build a state via ``testing.build_test_state`` and pass it here.
    """
    # static_folder=None disables Flask's default /static route; the shell serves its own
    # static assets explicitly below.
    application = Flask(__name__, static_folder=None)
    # The two browser-side modules are served to other origins on purpose (a CORS header below).
    install_request_guard(application, exempt_rules=frozenset({APP_CONTRACT_ROUTE, CONTEXT_MENU_ROUTE}))
    attach_state(application, state)
    attach_document_policy(application, {"img-src": f"'self' data: blob: {ONBOARDING_MEDIA_ORIGIN}"})
    application.register_error_handler(Exception, handle_unhandled_exception)
    sock = build_sock(application)

    application.add_url_rule("/", view_func=_index, methods=["GET"])
    application.add_url_rule("/favicon.ico", view_func=_favicon, methods=["GET"])
    application.add_url_rule(TOUCH_ICON_PATH, view_func=_touch_icon, methods=["GET"])
    application.add_url_rule(MANIFEST_PATH, view_func=_web_manifest, methods=["GET"])
    application.add_url_rule("/api/health", view_func=_health_endpoint, methods=["GET"])
    application.add_url_rule(RECOVERY_LAUNCH_PATH, view_func=_recovery_launch, methods=["POST"])
    application.add_url_rule(DESKTOPS_RECOVERY_PATH, view_func=_desktops_recovery_page, methods=["GET"])
    application.add_url_rule(THEME_GALLERY_ROUTE, view_func=_theme_gallery, methods=["GET"])
    register_api_schema_route(
        application,
        DEFAULT_API_SCHEMA_PATH,
        title=APP_DISPLAY_NAME,
        description=APP_DESCRIPTION,
        route_table=SHELL_ROUTE_TABLE,
    )
    application.add_url_rule(APP_CONTRACT_ROUTE, view_func=_serve_app_contract, methods=["GET"])
    application.add_url_rule(CONTEXT_MENU_ROUTE, view_func=_serve_context_menu, methods=["GET"])
    application.add_url_rule(PRESENCE_PATH, view_func=_presence_endpoint, methods=["GET"])
    application.add_url_rule(PRESENCE_HEARTBEAT_PATH, view_func=_presence_heartbeat_endpoint, methods=["POST"])
    application.add_url_rule(ROSTER_ROUTE, view_func=_roster_endpoint, methods=["GET"])
    application.add_url_rule(f"{ROSTER_ROUTE}/<key>/picture", view_func=_participant_picture_endpoint, methods=["GET"])
    register_shell_routes(application)
    register_keybindings_routes(application)
    register_avatar_routes(application)
    sock.route("/api/ws")(_ws_endpoint)

    # Registered unconditionally, even when the bundle is absent at startup: the directory can
    # appear later (a rebuild), and a route decided at construction time can never notice.
    application.add_url_rule("/assets/<path:filename>", view_func=_serve_asset, methods=["GET"])
    application.add_url_rule("/<path:path>", view_func=_index_catch_all, methods=["GET"])

    return application
