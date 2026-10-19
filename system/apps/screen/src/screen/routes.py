"""The service's routes (docs/system/specs/screen/spec.md section 10.1) over ``ScreenService``."""

import queue
from pathlib import Path
from typing import Any
from typing import Final

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from flask_sock import Sock
from loguru import logger
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import NotFound
from werkzeug.exceptions import RequestEntityTooLarge

from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import CONTEXT_MENU_ROUTE
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry import SHELL_CONTEXT_MENU_PATH
from app_schema.api_schema import DEFAULT_API_SCHEMA_PATH
from app_schema.api_schema import register_api_schema_route
from screen.browser_routes import CANCEL_PENDING_URL_RULE
from screen.browser_routes import OPEN_URL_PATH
from screen.browser_routes import build_browser_blueprint
from screen.data_types import Lifecycle
from screen.desktop_files import MAX_DROP_BYTES
from screen.errors import ClipboardError
from screen.errors import DisplayModeError
from screen.errors import FileDropError
from screen.errors import InvalidDroppedFileNameError
from screen.errors import InvalidRunCommandError
from screen.errors import NekoError
from screen.errors import ScreenError
from screen.errors import ScreenNotRunningError
from screen.errors import WindowToolError
from screen.identity import AGENT_ID_HEADER
from screen.identity import AGENT_NAME_HEADER
from screen.identity import IDENTITY_HEADER
from screen.identity import parse_identity_header
from screen.primitives import APP_DESCRIPTION
from screen.primitives import APP_DISPLAY_NAME
from screen.primitives import AgentId
from screen.primitives import DroppedFileName
from screen.primitives import LAUNCH_BROWSER_PATH
from screen.primitives import RunCommand
from screen.primitives import XWindowId
from screen.proxy import NEKO_PROXY_PREFIX
from screen.proxy import NekoProxy
from screen.route_guards import VISITOR_REFUSAL
from screen.route_guards import describe_controller_refusal
from screen.route_guards import describe_visitor_refusal
from screen.route_guards import is_owner_request
from screen.route_guards import session_id_from_request
from screen.service import DEFAULT_SCREENSHOT_QUALITY
from screen.service import ScreenService
from screen.watch import STOPPED_NOTIFICATION_PATH
from screen.xclipboard import MAX_CLIPBOARD_BYTES

PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"

HTTP_NO_CONTENT: Final[int] = 204
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_PAYLOAD_TOO_LARGE: Final[int] = 413
HTTP_UNSUPPORTED_MEDIA_TYPE: Final[int] = 415
HTTP_BAD_GATEWAY: Final[int] = 502
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

# How long ``screen run`` waits for a start it triggered before answering that the Screen is still starting.
_RUN_START_WAIT_SECONDS: Final[float] = 45.0
_VIEWER_QUEUE_POLL_SECONDS: Final[float] = 1.0
_VIEWER_INBOUND_POLL_SECONDS: Final[float] = 0.05

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Screen</title></head>'
    "<body><p>The Screen's viewer has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)

# A share admits a visitor to the whole origin, so the routes that act on the Screen outside the lease refuse one:
# they are the owner's and the agents' (who reach the service over loopback with no identity header), while the
# viewer's routes, where the lease arbitrates, stay open. Matched on the path and everything under it, except the
# rules below, which the lease arbitrates although they sit under an owner-only prefix.
_OWNER_ONLY_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/api/run",
    "/api/control/acquire",
    "/api/control/release",
    "/api/control/handoff",
    "/api/screenshot",
    "/api/windows",
    LAUNCH_BROWSER_PATH,
    OPEN_URL_PATH,
    "/browsers",
)
# Rules under an owner-only prefix that the lease arbitrates instead (a session in control, or the owner).
_LEASE_ARBITRATED_RULES: Final[frozenset[str]] = frozenset({CANCEL_PENDING_URL_RULE})
# The multipart fields of a drop and of a pasted image, beside ``session_id``.
_DROP_FILES_FIELD: Final[str] = "files"
_CLIPBOARD_IMAGE_FIELD: Final[str] = "image"


def _error(detail: str, status: int) -> Response:
    response = jsonify({"detail": detail, "error": detail})
    response.status_code = status
    return response


def _is_owner_only_path(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in _OWNER_ONLY_PATH_PREFIXES)


def _json_body() -> dict[str, Any]:
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def _agent_identity() -> tuple[AgentId | None, str | None]:
    # Advisory: a client-set header among cooperating in-container agents, not authentication.
    raw_id = request.headers.get(AGENT_ID_HEADER, "").strip()
    name = request.headers.get(AGENT_NAME_HEADER, "").strip() or None
    return (AgentId(raw_id) if raw_id else None), name


def _cap_request_body(max_bytes: int) -> None:
    """Have werkzeug refuse a body past ``max_bytes`` as it is read, framing included: a Content-Length past it before
    the first byte, and a chunked body (which names no length) the moment it passes the cap, so an oversize upload is
    never spooled to the tmpfs. Set before anything reads the body."""
    request.max_content_length = max_bytes


def _is_loopback_request() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def build_app(service: ScreenService, static_directory: Path, proxy: NekoProxy) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
    sock = Sock(app)
    register_api_schema_route(app, DEFAULT_API_SCHEMA_PATH, title=APP_DISPLAY_NAME, description=APP_DESCRIPTION)

    # the owner gate

    @app.before_request
    def refuse_visitors_on_owner_routes() -> ResponseReturnValue | None:
        if request.url_rule is not None and request.url_rule.rule in _LEASE_ARBITRATED_RULES:
            return None
        if _is_owner_only_path(request.path) and not is_owner_request():
            return _error(VISITOR_REFUSAL, HTTP_FORBIDDEN)
        return None

    @app.errorhandler(RequestEntityTooLarge)
    def refuse_oversize_body(_error_: RequestEntityTooLarge) -> ResponseReturnValue:
        # Raised past a route's body cap, and by werkzeug's own form limits (the memory a form's fields may take,
        # the number of parts) on any route that reads a form, where no body cap is set.
        cap = request.max_content_length
        detail = f"the body is over the {cap} byte cap" if cap is not None else "the body is too large to parse"
        return _error(detail, HTTP_PAYLOAD_TOO_LARGE)

    # the page and the shell's modules

    @app.get("/")
    def page() -> ResponseReturnValue:
        """The viewer page, from the frontend's build; a page load starts the stack when it is down."""
        service.ensure_started()
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        response = send_file(document.absolute(), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        """One file of the viewer's built bundle, by name."""
        try:
            return send_from_directory((static_directory / "assets").absolute(), filename)
        except NotFound:
            return Response(status=HTTP_NOT_FOUND)

    def _shell_module(path: Path) -> ResponseReturnValue:
        if not path.is_file():
            return _error(f"the workspace shell's frontend is not built: {path} is missing", HTTP_NOT_FOUND)
        return send_file(path.absolute(), mimetype="text/javascript")

    @app.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        """The app contract module: how the page talks to the shell framing it."""
        return _shell_module(SHELL_APP_CONTRACT_PATH)

    @app.get(CONTEXT_MENU_ROUTE)
    def context_menu() -> ResponseReturnValue:
        """The element context menu module: the right-click menu that hands an element on a page to a chat."""
        return _shell_module(SHELL_CONTEXT_MENU_PATH)

    @app.get("/health")
    def health() -> ResponseReturnValue:
        """Whether the service is up, with the stack's lifecycle."""
        return jsonify({"status": "ok", "lifecycle": service.stack.lifecycle.value.lower()})

    # state and the service's events

    @app.get("/api/state")
    def state() -> ResponseReturnValue:
        """The state document: the lifecycle, the lease, the frontmost window's title, the video profile, the
        Screen's size and the browser's state."""
        return jsonify(service.state_document())

    @app.post("/api/start")
    def start() -> ResponseReturnValue:
        """Start the stack when it is down, and answer the lifecycle."""
        lifecycle = service.ensure_started()
        return jsonify({"lifecycle": lifecycle.value.lower()})

    @app.get("/api/connect-info")
    def connect_info() -> ResponseReturnValue:
        """Where this viewer's WebRTC can reach the Screen (candidates) and the ICE servers to use; ``?relay=1`` asks
        the brokers for relay servers too."""
        is_relay = request.args.get("relay", "") == "1"
        response = jsonify(service.connectivity.fetch(is_relay).document())
        # A relay credential is a bearer token: nothing between the service and the viewer may keep it.
        response.headers["Cache-Control"] = "no-store"
        return response

    @sock.route("/api/ws")
    def events_socket(ws: Any) -> None:
        """The service's events to a viewer: ``state`` on connect and on every change, ``profile``, ``stopped`` and
        ``handoff_request``."""
        client_queue = service.broadcaster.register()
        try:
            ws.send(service.broadcaster_state_text())
            is_open = True
            while is_open:
                try:
                    message = client_queue.get(timeout=_VIEWER_QUEUE_POLL_SECONDS)
                except queue.Empty:
                    # A receive with a short timeout notices a closed socket between events.
                    ws.receive(timeout=_VIEWER_INBOUND_POLL_SECONDS)
                    continue
                if message is None:
                    is_open = False
                else:
                    ws.send(message)
        except ConnectionClosed as e:
            # A page going away is the normal end of its events socket.
            logger.trace("A viewer's events socket closed: {!r}", e)
        finally:
            service.broadcaster.unregister(client_queue)

    # sessions and the human verbs

    @app.post("/api/session")
    def create_session() -> ResponseReturnValue:
        """Log the requester in to neko as a session named from ``X-Imbue-Identity`` (a header-less request is the
        owner); answers the session id, its token and name, and whether it is the owner's."""
        identity = parse_identity_header(request.headers.get(IDENTITY_HEADER))
        try:
            session = service.create_session(identity)
        except ScreenNotRunningError as e:
            service.ensure_started()
            return _error(str(e), HTTP_SERVICE_UNAVAILABLE)
        except NekoError as e:
            logger.warning("Could not log a viewer in to neko: {}", e)
            return _error(f"neko refused the login: {e}", HTTP_BAD_GATEWAY)
        return jsonify(
            {
                "session_id": session.session_id,
                "token": str(session.token),
                "name": str(session.name),
                "is_owner": session.is_owner,
            }
        )

    @app.post("/api/control/take")
    def take_control() -> ResponseReturnValue:
        """The human verb: the session named by ``session_id`` takes control of the Screen through the lease."""
        session_id = session_id_from_request()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = describe_visitor_refusal(service, session_id)
        if refusal is not None:
            return _error(refusal, HTTP_FORBIDDEN)
        if service.stack.lifecycle != Lifecycle.RUNNING:
            return _error("the Screen is not running", HTTP_CONFLICT)
        if not service.take_control(session_id):
            return _error("no such session", HTTP_NOT_FOUND)
        return jsonify({"ok": True, **service.state_document()["control"]})

    @app.post("/api/control/return")
    def return_control() -> ResponseReturnValue:
        """The human verb: the session named by ``session_id`` gives control of the Screen back."""
        session_id = session_id_from_request()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = describe_visitor_refusal(service, session_id)
        if refusal is not None:
            return _error(refusal, HTTP_FORBIDDEN)
        returned = service.return_control(session_id)
        return jsonify({"ok": returned, **service.state_document()["control"]})

    # the agent verbs (the fleet API's shape)

    @app.post("/api/control/acquire")
    def acquire() -> ResponseReturnValue:
        """The agent verb: the agent named by ``X-Mngr-Agent-Id`` acquires the lease (``reclaim`` to take it back)."""
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _error(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        answer = service.acquire(agent_id, agent_name, is_reclaim=bool(_json_body().get("reclaim", False)))
        if answer["ok"]:
            service.touch(agent_id)
        return jsonify(answer)

    @app.post("/api/control/release")
    def release() -> ResponseReturnValue:
        """The agent verb: the agent named by ``X-Mngr-Agent-Id`` releases the lease."""
        agent_id, _ = _agent_identity()
        if agent_id is None:
            return _error(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        return jsonify({"released": service.release(agent_id), **service.state_document()["control"]})

    @app.post("/api/control/handoff")
    def handoff() -> ResponseReturnValue:
        """The agent verb: the agent holding the lease hands the Screen to the human, with a ``reason``."""
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _error(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        reason = str(_json_body().get("reason", "")).strip() or "human verification needed"
        return jsonify(service.handoff(agent_id, agent_name, reason))

    # sizing and the profile's input clock

    @app.post("/api/screen-size")
    def screen_size() -> ResponseReturnValue:
        """Resize the Screen to the controller's stage size (``width`` and ``height`` in CSS pixels); a report from a
        session that does not control the Screen is not applied."""
        body = _json_body()
        width, height = body.get("width"), body.get("height")
        if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
            return _error("width and height must be positive integers", HTTP_BAD_REQUEST)
        if service.stack.lifecycle != Lifecycle.RUNNING:
            return _error("the Screen is not running", HTTP_CONFLICT)
        session_id = session_id_from_request()
        current = service.screen_size
        # A viewer's report counts only while its session controls the Screen; a report with no session (the
        # ``screen size`` command) is the controller's by definition (spec sections 7.2 and 17), so only the owner
        # may send one.
        if session_id is None and not is_owner_request():
            return _error(VISITOR_REFUSAL, HTTP_FORBIDDEN)
        if session_id is not None:
            refusal = describe_visitor_refusal(service, session_id)
            if refusal is not None:
                return _error(refusal, HTTP_FORBIDDEN)
        if session_id is not None and not service.is_controller_session(session_id):
            return jsonify(
                {
                    "applied": False,
                    "reason": "not the controller",
                    "screen_size": current.model_dump() if current is not None else None,
                }
            )
        try:
            applied = service.set_screen_size(width, height)
        except ScreenNotRunningError as e:
            return _error(str(e), HTTP_CONFLICT)
        except DisplayModeError as e:
            return _error(str(e), HTTP_BAD_GATEWAY)
        return jsonify({"applied": True, "reason": None, "screen_size": applied.model_dump()})

    @app.post("/api/input-seen")
    def input_seen() -> ResponseReturnValue:
        """The viewer's input relay reporting activity for its session, the profile chooser's clock."""
        session_id = session_id_from_request()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = describe_visitor_refusal(service, session_id)
        if refusal is not None:
            return _error(refusal, HTTP_FORBIDDEN)
        if not service.note_input(session_id):
            return _error("no such session", HTTP_NOT_FOUND)
        return Response(status=HTTP_NO_CONTENT)

    # the clipboard

    @app.post("/api/clipboard/image")
    def clipboard_image() -> ResponseReturnValue:
        """The controller's pasted image (spec section 13): multipart ``session_id`` and ``image``."""
        _cap_request_body(MAX_CLIPBOARD_BYTES)
        if service.stack.lifecycle != Lifecycle.RUNNING:
            return _error("the Screen is not running", HTTP_CONFLICT)
        session_id = session_id_from_request()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = describe_controller_refusal(service, session_id)
        if refusal is not None:
            return _error(refusal, HTTP_FORBIDDEN)
        upload = request.files.get(_CLIPBOARD_IMAGE_FIELD)
        if upload is None:
            return _error(f"the {_CLIPBOARD_IMAGE_FIELD} part is missing", HTTP_BAD_REQUEST)
        mime = upload.mimetype.lower()
        if not mime.startswith("image/"):
            return _error("the image part must be an image (image/...)", HTTP_UNSUPPORTED_MEDIA_TYPE)
        data = upload.read()
        if len(data) == 0:
            return _error("the image is empty", HTTP_BAD_REQUEST)
        try:
            service.set_clipboard_image(data, mime)
        except ScreenNotRunningError as e:
            return _error(str(e), HTTP_CONFLICT)
        except ClipboardError as e:
            return _error(str(e), HTTP_BAD_GATEWAY)
        return Response(status=HTTP_NO_CONTENT)

    # files dropped on the viewer (spec section 4.3)

    @app.post("/api/drop")
    def drop_files() -> ResponseReturnValue:
        """The controller's dropped files (multipart ``session_id`` and ``files``), written into the Screen's desktop
        folder under their own names."""
        _cap_request_body(MAX_DROP_BYTES)
        if service.stack.lifecycle != Lifecycle.RUNNING:
            return _error("the Screen is not running", HTTP_CONFLICT)
        session_id = session_id_from_request()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = describe_controller_refusal(service, session_id)
        if refusal is not None:
            return _error(refusal, HTTP_FORBIDDEN)
        uploads = request.files.getlist(_DROP_FILES_FIELD)
        if not uploads:
            return _error("no files were sent", HTTP_BAD_REQUEST)
        try:
            named = [(DroppedFileName(upload.filename or ""), upload.stream) for upload in uploads]
        except InvalidDroppedFileNameError as e:
            return _error(str(e), HTTP_BAD_REQUEST)
        try:
            saved = service.save_dropped_files(named)
        except ScreenNotRunningError as e:
            return _error(str(e), HTTP_CONFLICT)
        except FileDropError as e:
            logger.warning("A dropped file could not be saved: {}", e)
            return _error(str(e), HTTP_BAD_GATEWAY)
        return jsonify({"directory": str(service.desktop_dir), "saved": [path.name for path in saved]})

    # the display

    @app.get("/api/screenshot")
    def screenshot() -> ResponseReturnValue:
        """The display as an image (``format`` png or jpeg, ``quality`` 1 to 100), from neko's screenshot route."""
        raw_quality = request.args.get("quality", str(DEFAULT_SCREENSHOT_QUALITY))
        if not raw_quality.isdigit() or not 1 <= int(raw_quality) <= 100:
            return _error("quality must be an integer from 1 to 100", HTTP_BAD_REQUEST)
        image_format = request.args.get("format", "png")
        if image_format not in ("png", "jpeg"):
            return _error("format must be png or jpeg", HTTP_BAD_REQUEST)
        agent_id, _ = _agent_identity()
        if agent_id is not None:
            service.touch(agent_id)
        try:
            image = service.screenshot(int(raw_quality), is_png=image_format == "png")
        except ScreenNotRunningError as e:
            return _error(str(e), HTTP_CONFLICT)
        except NekoError as e:
            return _error(f"neko could not take the screenshot: {e}", HTTP_BAD_GATEWAY)
        return Response(image, mimetype="image/png" if image_format == "png" else "image/jpeg")

    @app.get("/api/windows")
    def windows() -> ResponseReturnValue:
        """The X windows on the display, through ``wmctrl``."""
        agent_id, _ = _agent_identity()
        if agent_id is not None:
            service.touch(agent_id)
        try:
            listed = service.list_windows()
        except ScreenNotRunningError as e:
            return _error(str(e), HTTP_CONFLICT)
        except WindowToolError as e:
            return _error(str(e), HTTP_BAD_GATEWAY)
        return jsonify({"windows": [window.model_dump() for window in listed]})

    @app.post("/api/windows/<window_id>/activate")
    def activate_window(window_id: str) -> ResponseReturnValue:
        """Raise and focus the X window with this id."""
        agent_id, _ = _agent_identity()
        if agent_id is not None:
            service.touch(agent_id)
        try:
            service.activate_window(XWindowId(window_id))
        except ScreenNotRunningError as e:
            return _error(str(e), HTTP_CONFLICT)
        except WindowToolError as e:
            return _error(str(e), HTTP_BAD_REQUEST)
        return jsonify({"activated": True})

    @app.post("/api/run")
    def run() -> ResponseReturnValue:
        """Launch a ``command`` on the display, detached, starting the stack first when it is down; answers the pid."""
        agent_id, _ = _agent_identity()
        try:
            command = RunCommand(_json_body().get("command"))
        except InvalidRunCommandError as e:
            return _error(str(e), HTTP_BAD_REQUEST)
        if service.stack.lifecycle != Lifecycle.RUNNING:
            service.ensure_started()
            if not service.wait_until_running(_RUN_START_WAIT_SECONDS):
                return _error("the Screen is still starting; try again in a moment", HTTP_SERVICE_UNAVAILABLE)
        if agent_id is not None:
            service.touch(agent_id)
        try:
            pid = service.run_command(command)
        except ScreenError as e:
            return _error(str(e), HTTP_BAD_GATEWAY)
        return jsonify({"pid": pid})

    # the browser: its launch path, the open:url handler and the fleet API (spec section 16)

    app.register_blueprint(build_browser_blueprint(service))

    # the watcher's notification

    @app.post(STOPPED_NOTIFICATION_PATH)
    def stopped() -> ResponseReturnValue:
        """The watcher's notification that a member of the stack exited (loopback only): the service records the stop
        with its reason."""
        if not _is_loopback_request():
            return _error("loopback only", HTTP_FORBIDDEN)
        body = _json_body()
        member = str(body.get("member", "")).strip() or "a member"
        reason = str(body.get("reason", "")).strip() or f"{member} stopped"
        if not service.notify_stopped(member, reason):
            return _error("the group is still running", HTTP_CONFLICT)
        return Response(status=HTTP_NO_CONTENT)

    # the neko proxy

    @app.route(f"{NEKO_PROXY_PREFIX}/<path:upstream_path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    def neko_http(upstream_path: str) -> ResponseReturnValue:
        """The reverse proxy to neko's HTTP API."""
        return proxy.relay_http(request, upstream_path)

    @sock.route(f"{NEKO_PROXY_PREFIX}/api/ws")
    def neko_socket(ws: Any) -> None:
        """The reverse proxy to neko's WebSocket."""
        proxy.relay_websocket(ws, request.query_string.decode("latin-1"))

    return app
