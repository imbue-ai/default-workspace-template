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

from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import CONTEXT_MENU_ROUTE
from app_manifest.registry import SHELL_APP_CONTRACT_PATH
from app_manifest.registry import SHELL_CONTEXT_MENU_PATH
from screen.browser_routes import LAUNCH_BROWSER_PATH
from screen.browser_routes import OPEN_URL_PATH
from screen.browser_routes import build_browser_blueprint
from screen.data_types import Lifecycle
from screen.errors import ClipboardError
from screen.errors import DisplayModeError
from screen.errors import InvalidRunCommandError
from screen.errors import NekoError
from screen.errors import ScreenError
from screen.errors import ScreenNotRunningError
from screen.errors import WindowToolError
from screen.identity import AGENT_ID_HEADER
from screen.identity import AGENT_NAME_HEADER
from screen.identity import IDENTITY_HEADER
from screen.identity import parse_identity_header
from screen.primitives import AgentId
from screen.primitives import NekoSessionId
from screen.primitives import RunCommand
from screen.primitives import XWindowId
from screen.proxy import NEKO_PROXY_PREFIX
from screen.proxy import NekoProxy
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
# viewer's routes, where the lease arbitrates, stay open. Matched on the path and everything under it.
_OWNER_ONLY_PATH_PREFIXES: Final[tuple[str, ...]] = (
    "/api/run",
    "/api/control/acquire",
    "/api/control/release",
    "/api/control/handoff",
    "/api/screenshot",
    "/api/windows",
    "/api/clipboard/image",
    LAUNCH_BROWSER_PATH,
    OPEN_URL_PATH,
    "/browsers",
)
_VISITOR_REFUSAL: Final[str] = "this route is the workspace owner's"


def _error(detail: str, status: int) -> Response:
    response = jsonify({"detail": detail, "error": detail})
    response.status_code = status
    return response


def _is_owner_request() -> bool:
    return parse_identity_header(request.headers.get(IDENTITY_HEADER)).owner


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


def _session_id_from_body() -> NekoSessionId | None:
    raw = _json_body().get("session_id")
    return NekoSessionId(raw) if isinstance(raw, str) and raw else None


def _visitor_refusal_for_session(service: ScreenService, session_id: NekoSessionId) -> Response | None:
    """A visitor acts only as a visitor's session: naming the owner's, whose id the state document carries while
    it controls, is refused."""
    if _is_owner_request():
        return None
    session = service.known_session(session_id)
    if session is not None and session.is_owner:
        return _error(_VISITOR_REFUSAL, HTTP_FORBIDDEN)
    return None


def _is_loopback_request() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


def build_app(service: ScreenService, static_directory: Path, proxy: NekoProxy) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
    sock = Sock(app)

    # the owner gate

    @app.before_request
    def refuse_visitors_on_owner_routes() -> ResponseReturnValue | None:
        if _is_owner_only_path(request.path) and not _is_owner_request():
            return _error(_VISITOR_REFUSAL, HTTP_FORBIDDEN)
        return None

    # the page and the shell's modules

    @app.get("/")
    def page() -> ResponseReturnValue:
        service.ensure_started()
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        response = send_file(document.absolute(), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        try:
            return send_from_directory((static_directory / "assets").absolute(), filename)
        except NotFound:
            return Response(status=HTTP_NOT_FOUND)

    def _shell_module(path: Path) -> ResponseReturnValue:
        if not path.is_file():
            return _error(f"the workspace shell's frontend is not built: {path} is missing", HTTP_NOT_FOUND)
        return send_file(path.absolute(), mimetype="text/javascript")

    app.add_url_rule(APP_CONTRACT_ROUTE, "app_contract", lambda: _shell_module(SHELL_APP_CONTRACT_PATH))
    app.add_url_rule(CONTEXT_MENU_ROUTE, "context_menu", lambda: _shell_module(SHELL_CONTEXT_MENU_PATH))

    @app.get("/health")
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok", "lifecycle": service.stack.lifecycle.value.lower()})

    # state and the service's events

    @app.get("/api/state")
    def state() -> ResponseReturnValue:
        return jsonify(service.state_document())

    @app.post("/api/start")
    def start() -> ResponseReturnValue:
        lifecycle = service.ensure_started()
        return jsonify({"lifecycle": lifecycle.value.lower()})

    @app.get("/api/connect-info")
    def connect_info() -> ResponseReturnValue:
        is_relay = request.args.get("relay", "") == "1"
        response = jsonify(service.connectivity.fetch(is_relay).document())
        # A relay credential is a bearer token: nothing between the service and the viewer may keep it.
        response.headers["Cache-Control"] = "no-store"
        return response

    @sock.route("/api/ws")
    def events_socket(ws: Any) -> None:
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
        session_id = _session_id_from_body()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = _visitor_refusal_for_session(service, session_id)
        if refusal is not None:
            return refusal
        if service.stack.lifecycle != Lifecycle.RUNNING:
            return _error("the Screen is not running", HTTP_CONFLICT)
        if not service.take_control(session_id):
            return _error("no such session", HTTP_NOT_FOUND)
        return jsonify({"ok": True, **service.state_document()["control"]})

    @app.post("/api/control/return")
    def return_control() -> ResponseReturnValue:
        session_id = _session_id_from_body()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = _visitor_refusal_for_session(service, session_id)
        if refusal is not None:
            return refusal
        returned = service.return_control(session_id)
        return jsonify({"ok": returned, **service.state_document()["control"]})

    # the agent verbs (the fleet API's shape)

    @app.post("/api/control/acquire")
    def acquire() -> ResponseReturnValue:
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _error(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        answer = service.acquire(agent_id, agent_name, is_reclaim=bool(_json_body().get("reclaim", False)))
        if answer["ok"]:
            service.touch(agent_id)
        return jsonify(answer)

    @app.post("/api/control/release")
    def release() -> ResponseReturnValue:
        agent_id, _ = _agent_identity()
        if agent_id is None:
            return _error(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        return jsonify({"released": service.release(agent_id), **service.state_document()["control"]})

    @app.post("/api/control/handoff")
    def handoff() -> ResponseReturnValue:
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _error(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        reason = str(_json_body().get("reason", "")).strip() or "human verification needed"
        return jsonify(service.handoff(agent_id, agent_name, reason))

    # sizing and the profile's input clock

    @app.post("/api/screen-size")
    def screen_size() -> ResponseReturnValue:
        body = _json_body()
        width, height = body.get("width"), body.get("height")
        if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
            return _error("width and height must be positive integers", HTTP_BAD_REQUEST)
        if service.stack.lifecycle != Lifecycle.RUNNING:
            return _error("the Screen is not running", HTTP_CONFLICT)
        session_id = _session_id_from_body()
        current = service.screen_size
        # A viewer's report counts only while its session controls the Screen; a report with no session (the
        # ``screen size`` command) is the controller's by definition (spec sections 7.2 and 17), so only the owner
        # may send one.
        if session_id is None and not _is_owner_request():
            return _error(_VISITOR_REFUSAL, HTTP_FORBIDDEN)
        if session_id is not None:
            refusal = _visitor_refusal_for_session(service, session_id)
            if refusal is not None:
                return refusal
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
        session_id = _session_id_from_body()
        if session_id is None:
            return _error("session_id is required", HTTP_BAD_REQUEST)
        refusal = _visitor_refusal_for_session(service, session_id)
        if refusal is not None:
            return refusal
        if not service.note_input(session_id):
            return _error("no such session", HTTP_NOT_FOUND)
        return Response(status=HTTP_NO_CONTENT)

    # the clipboard

    @app.post("/api/clipboard/image")
    def clipboard_image() -> ResponseReturnValue:
        mime = (request.content_type or "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            return _error("the body must be an image (Content-Type image/...)", HTTP_UNSUPPORTED_MEDIA_TYPE)
        if request.content_length is not None and request.content_length > MAX_CLIPBOARD_BYTES:
            return _error(f"the image is over the {MAX_CLIPBOARD_BYTES} byte cap", HTTP_PAYLOAD_TOO_LARGE)
        data = request.get_data()
        if len(data) == 0:
            return _error("the image is empty", HTTP_BAD_REQUEST)
        if len(data) > MAX_CLIPBOARD_BYTES:
            return _error(f"the image is over the {MAX_CLIPBOARD_BYTES} byte cap", HTTP_PAYLOAD_TOO_LARGE)
        try:
            service.set_clipboard_image(data, mime)
        except ScreenNotRunningError as e:
            return _error(str(e), HTTP_CONFLICT)
        except ClipboardError as e:
            return _error(str(e), HTTP_BAD_GATEWAY)
        return Response(status=HTTP_NO_CONTENT)

    # the display

    @app.get("/api/screenshot")
    def screenshot() -> ResponseReturnValue:
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
        return proxy.relay_http(request, upstream_path)

    @sock.route(f"{NEKO_PROXY_PREFIX}/api/ws")
    def neko_socket(ws: Any) -> None:
        proxy.relay_websocket(ws, request.query_string.decode("latin-1"))

    return app
