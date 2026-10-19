"""The migrated fleet API (docs/system/specs/screen/spec.md sections 10.1 and 16): the ``/browsers`` routes the
fleet CLI drives, the ``browser`` launch path and the ``open:url`` handler, as a blueprint over ``ScreenService``."""

from typing import Any
from typing import Final

from flask import Blueprint
from flask import Response
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger

from screen.browser_fleet import Browser
from screen.errors import BrowserNotDrivableError
from screen.errors import DuplicateBrowserNameError
from screen.errors import FleetFullError
from screen.errors import InvalidBrowserNameError
from screen.errors import InvalidStartUrlError
from screen.errors import UnknownBrowserError
from screen.identity import AGENT_ID_HEADER
from screen.identity import AGENT_NAME_HEADER
from screen.primitives import APP_NAME
from screen.primitives import AbsoluteHttpUrl
from screen.primitives import AgentId
from screen.primitives import BrowserName
from screen.primitives import LAUNCH_BROWSER_PATH
from screen.primitives import LocalHttpUrl
from screen.primitives import NekoSessionId
from screen.route_guards import CONTROLLER_REFUSAL
from screen.route_guards import describe_visitor_refusal
from screen.route_guards import is_owner_request
from screen.route_guards import session_id_from_request
from screen.service import ScreenService
from workspace_layout.client import ShellLayoutClient
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.errors import ShellOpError
from workspace_layout.ops import ShowArgs
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import WindowPath
from workspace_layout.shell_url import shell_base_url

OPEN_URL_PATH: Final[str] = "/api/open-url"
# The one fleet rule the lease arbitrates rather than the owner gate: dropping a held page is the controller's or
# the owner's (section 4.2), and the owner over loopback carries no session.
CANCEL_PENDING_URL_RULE: Final[str] = "/browsers/<raw_name>/pending-url/cancel"
# The launch path's one parameter (the manifest's ``params``): the start page, posted by the shell beside its own
# envelope fields, which are ignored here.
START_URL_PARAM: Final[str] = "url"
# The Screen's one page, which the launch path answers and the open:url handler shows.
SCREEN_PAGE_PATH: Final[str] = "/"
# Short, since the shell answers a ``show`` at once and the relay that posted the message bounds the delivery.
_SHELL_SHOW_TIMEOUT_SECONDS: Final[float] = 5.0

HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_BAD_GATEWAY: Final[int] = 502
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503


def _error(payload: dict[str, Any], status: int) -> Response:
    response = jsonify(payload)
    response.status_code = status
    return response


def _refusal(reason: str, status: int) -> Response:
    """A refusal of a launch path or a message handler, as the shell reads one: the reason under ``detail``."""
    return _error({"detail": reason}, status)


def _body() -> dict[str, Any]:
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def _agent_identity() -> tuple[AgentId | None, str | None]:
    # Advisory: a client-set header among cooperating in-container agents, not authentication.
    raw_id = request.headers.get(AGENT_ID_HEADER, "").strip()
    name = request.headers.get(AGENT_NAME_HEADER, "").strip() or None
    return (AgentId(raw_id) if raw_id else None), name


def _browser_name(raw: str) -> BrowserName | None:
    try:
        return BrowserName(raw)
    except InvalidBrowserNameError:
        return None


def build_browser_blueprint(service: ScreenService) -> Blueprint:
    blueprint = Blueprint("browser", __name__)

    def _resolve(raw_name: str) -> Browser | Response:
        name = _browser_name(raw_name)
        if name is None:
            return _error({"error": "invalid browser name"}, HTTP_NOT_FOUND)
        try:
            return service.fleet.get(name)
        except UnknownBrowserError as e:
            return _error({"error": str(e)}, HTTP_NOT_FOUND)

    def _start_browser(name: str | None, raw_url: str | None, is_launch_path: bool) -> Browser | Response:
        """The browser a nameless request means (the one that exists, started again if stopped, else a new one),
        or the named create, answered at once while Chromium launches in the background. A nameless request is the
        human's ``browser`` launch path (its start page held while an agent drives) or an agent's ``new`` (its page
        opened at once), as ``is_launch_path`` says."""
        missing = service.fleet.install_reason()
        if missing is not None:
            return _error({"error": missing}, HTTP_SERVICE_UNAVAILABLE)
        start_url: str | None = None
        if raw_url:
            try:
                start_url = str(AbsoluteHttpUrl(raw_url))
            except InvalidStartUrlError as e:
                return _error({"error": f"url: {e}"}, HTTP_BAD_REQUEST)
        try:
            if name is None:
                return service.launch_browser(start_url) if is_launch_path else service.new_browser(start_url)
            service.ensure_started()
            return service.fleet.create(name, start_url)
        except InvalidBrowserNameError as e:
            return _error({"error": str(e)}, HTTP_BAD_REQUEST)
        except (DuplicateBrowserNameError, FleetFullError) as e:
            return _error({"error": str(e)}, HTTP_CONFLICT)
        except BrowserNotDrivableError as e:
            return _error({"error": str(e)}, HTTP_CONFLICT)

    # the launch path and the message handler

    @blueprint.post(LAUNCH_BROWSER_PATH)
    def launch_browser() -> ResponseReturnValue:
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return _refusal("the launch body must be a JSON object", HTTP_BAD_REQUEST)
        raw_url = body.get(START_URL_PARAM)
        if raw_url is not None and not isinstance(raw_url, str):
            return _refusal("url: must be a string", HTTP_BAD_REQUEST)
        started = _start_browser(None, raw_url, is_launch_path=True)
        if isinstance(started, Response):
            return _refusal(
                str(started.get_json().get("error", "the browser could not be started")), started.status_code
            )
        return jsonify({"path": SCREEN_PAGE_PATH})

    @blueprint.post(OPEN_URL_PATH)
    def open_url() -> ResponseReturnValue:
        """A local page the human asked for: a tab in Chromium (held while an agent drives), and the Screen's page
        shown to the client that clicked."""
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return _refusal("the message body must be a JSON object", HTTP_BAD_REQUEST)
        raw_url = body.get(START_URL_PARAM)
        if not isinstance(raw_url, str):
            return _refusal("url: must be a string", HTTP_BAD_REQUEST)
        try:
            url = LocalHttpUrl(raw_url)
        except InvalidStartUrlError as e:
            return _refusal(f"url: {e}", HTTP_BAD_REQUEST)
        raw_client_id = body.get("client_id")
        try:
            client_id = ClientId(raw_client_id if isinstance(raw_client_id, str) else "")
        except InvalidLayoutValueError as e:
            return _refusal(f"client_id: {e}", HTTP_BAD_REQUEST)
        missing = service.fleet.install_reason()
        if missing is not None:
            return _refusal(missing, HTTP_SERVICE_UNAVAILABLE)
        try:
            browser = service.open_url_for_human(str(url))
        except FleetFullError as e:
            return _refusal(str(e), HTTP_CONFLICT)
        except BrowserNotDrivableError as e:
            return _refusal(f"The browser could not open the page: {e}", HTTP_CONFLICT)
        shell = ShellLayoutClient(
            shell_url=shell_base_url(), requester=None, timeout_seconds=_SHELL_SHOW_TIMEOUT_SECONDS
        )
        try:
            shown = shell.show(ShowArgs(app=APP_NAME, path=WindowPath(SCREEN_PAGE_PATH), client=client_id))
        except ShellOpError as e:
            logger.warning("Opened {} in browser {} but could not show the Screen's window: {}", url, browser.name, e)
            return _refusal(
                f"The browser has the page, but the Screen's window could not be shown: {e}", HTTP_BAD_GATEWAY
            )
        return jsonify({"browser": str(browser.name), "window_id": shown.window_id, "shown": shown.shown})

    # the fleet

    @blueprint.get("/browsers")
    def list_browsers() -> ResponseReturnValue:
        missing = service.fleet.install_reason()
        view = service.control.view()
        return jsonify(
            {
                "browsers": [service.fleet.describe(browser, view) for browser in service.fleet.browsers()],
                "can_create": missing is None,
                "create_reason": missing or "",
                "browser_count": service.fleet.launched_count(),
                "browser_max": service.fleet.max_browsers,
            }
        )

    @blueprint.post("/browsers")
    def create_browser() -> ResponseReturnValue:
        body = _body()
        raw_name = body.get("name")
        raw_url = body.get("url")
        started = _start_browser(
            None if raw_name is None else str(raw_name),
            None if raw_url is None else str(raw_url),
            is_launch_path=False,
        )
        if isinstance(started, Response):
            return started
        return jsonify({"name": str(started.name)})

    @blueprint.delete("/browsers/<raw_name>")
    def close_browser(raw_name: str) -> ResponseReturnValue:
        name = _browser_name(raw_name)
        if name is None:
            return _error({"error": "invalid browser name"}, HTTP_NOT_FOUND)
        service.fleet.close_and_forget(name)
        return jsonify({"closed": True})

    @blueprint.post("/browsers/<raw_name>/stop")
    def stop_browser(raw_name: str) -> ResponseReturnValue:
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        try:
            service.fleet.stop_browser(browser.name)
        except BrowserNotDrivableError as e:
            return _error({"error": str(e)}, HTTP_CONFLICT)
        return jsonify({"stopped": True})

    @blueprint.post("/browsers/<raw_name>/start")
    def start_browser(raw_name: str) -> ResponseReturnValue:
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        missing = service.fleet.install_reason()
        if missing is not None:
            return _error({"error": missing}, HTTP_SERVICE_UNAVAILABLE)
        service.ensure_started()
        try:
            service.fleet.start_browser(browser.name)
        except FleetFullError as e:
            return _error({"error": str(e)}, HTTP_CONFLICT)
        return jsonify({"started": True})

    @blueprint.post("/browsers/<raw_name>/close-tab")
    def close_active_tab(raw_name: str) -> ResponseReturnValue:
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        try:
            service.fleet.close_active_tab(browser.name)
        except BrowserNotDrivableError as e:
            return _error({"error": str(e)}, HTTP_CONFLICT)
        return jsonify({"closed": True})

    def _describe_cancel_refusal(session_id: NekoSessionId | None) -> str | None:
        if session_id is None:
            return None if is_owner_request() else CONTROLLER_REFUSAL
        visitor_refusal = describe_visitor_refusal(service, session_id)
        if visitor_refusal is not None:
            return visitor_refusal
        if is_owner_request() or service.is_controller_session(session_id):
            return None
        return CONTROLLER_REFUSAL

    @blueprint.post(CANCEL_PENDING_URL_RULE)
    def cancel_pending_url(raw_name: str) -> ResponseReturnValue:
        """Drop the held page: the session in control may (``session_id`` in the body or the query), and so may the
        owner, with or without a session, since agents and the shell reach the service over loopback with none."""
        refusal = _describe_cancel_refusal(session_id_from_request())
        if refusal is not None:
            return _error({"detail": refusal, "error": refusal}, HTTP_FORBIDDEN)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        return jsonify({"cancelled": service.fleet.cancel_pending_url(browser.name)})

    @blueprint.get("/browsers/<raw_name>/attach")
    def attach(raw_name: str) -> ResponseReturnValue:
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _error({"error": f"{AGENT_ID_HEADER} header required"}, HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        return jsonify(service.fleet.attach_for(browser.name, agent_id, agent_name))

    @blueprint.post("/browsers/<raw_name>/acquire")
    def acquire(raw_name: str) -> ResponseReturnValue:
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _error({"error": f"{AGENT_ID_HEADER} header required"}, HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        if not browser.is_running:
            return jsonify(service.fleet.attach_for(browser.name, agent_id, agent_name))
        answer = service.acquire(agent_id, agent_name, is_reclaim=bool(_body().get("reclaim", False)))
        if answer["ok"]:
            service.touch(agent_id)
        return jsonify({**answer, "lifecycle": browser.lifecycle.value.lower()})

    @blueprint.post("/browsers/<raw_name>/release")
    def release(raw_name: str) -> ResponseReturnValue:
        agent_id, _ = _agent_identity()
        if agent_id is None:
            return _error({"error": f"{AGENT_ID_HEADER} header required"}, HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        return jsonify({"released": service.release(agent_id)})

    @blueprint.post("/browsers/<raw_name>/handoff")
    def handoff(raw_name: str) -> ResponseReturnValue:
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _error({"error": f"{AGENT_ID_HEADER} header required"}, HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        reason = str(_body().get("reason", "")).strip() or "human verification needed"
        return jsonify(service.handoff(agent_id, agent_name, reason))

    return blueprint
