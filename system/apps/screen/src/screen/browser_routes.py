"""The migrated fleet API (docs/system/specs/screen/spec.md sections 10.1 and 16): the ``/browsers`` routes the
fleet CLI drives, the ``browser`` launch path and the ``open:url`` handler, as a blueprint over ``ScreenService``."""

from typing import Final

from flask import Blueprint
from flask import Response
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger

from app_schema.api_schema import AlternateResponse
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.routes import error_response
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from screen.api_models import AcquireRequest
from screen.api_models import AttachAnswer
from screen.api_models import BrowserAcquireAnswer
from screen.api_models import BrowserNameAnswer
from screen.api_models import BrowsersAnswer
from screen.api_models import CancelledAnswer
from screen.api_models import ClosedAnswer
from screen.api_models import CreateBrowserRequest
from screen.api_models import HandoffAnswer
from screen.api_models import HandoffRequest
from screen.api_models import LaunchAnswer
from screen.api_models import LaunchBrowserRequest
from screen.api_models import OpenUrlAnswer
from screen.api_models import OpenUrlRequest
from screen.api_models import ReleasedAnswer
from screen.api_models import SessionRequest
from screen.api_models import StartedAnswer
from screen.api_models import StoppedAnswer
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

# What the fleet's routes exchange (spec sections 10.1 and 16): the lease verbs under the fleet's paths answer the
# same documents the Screen's own verbs do, and an acquire over a browser that is not running answers the attach.
_BROWSER_RULE: Final[str] = "/browsers/{raw_name}"
BROWSER_ROUTE_TABLE: Final[RouteTable] = {
    ("POST", LAUNCH_BROWSER_PATH): RouteModels(request_model=LaunchBrowserRequest, response_model=LaunchAnswer),
    ("POST", OPEN_URL_PATH): RouteModels(request_model=OpenUrlRequest, response_model=OpenUrlAnswer),
    ("GET", "/browsers"): RouteModels(response_model=BrowsersAnswer),
    ("POST", "/browsers"): RouteModels(request_model=CreateBrowserRequest, response_model=BrowserNameAnswer),
    ("DELETE", _BROWSER_RULE): RouteModels(response_model=ClosedAnswer),
    ("POST", f"{_BROWSER_RULE}/stop"): RouteModels(response_model=StoppedAnswer),
    ("POST", f"{_BROWSER_RULE}/start"): RouteModels(response_model=StartedAnswer),
    ("POST", f"{_BROWSER_RULE}/close-tab"): RouteModels(response_model=ClosedAnswer),
    ("POST", f"{_BROWSER_RULE}/pending-url/cancel"): RouteModels(
        request_model=SessionRequest, response_model=CancelledAnswer, is_body_parsed_by_view=True
    ),
    ("GET", f"{_BROWSER_RULE}/attach"): RouteModels(response_model=AttachAnswer),
    ("POST", f"{_BROWSER_RULE}/acquire"): RouteModels(
        request_model=AcquireRequest,
        response_model=BrowserAcquireAnswer,
        alternate_responses=(AlternateResponse(status=200, model=AttachAnswer),),
    ),
    ("POST", f"{_BROWSER_RULE}/release"): RouteModels(response_model=ReleasedAnswer),
    ("POST", f"{_BROWSER_RULE}/handoff"): RouteModels(request_model=HandoffRequest, response_model=HandoffAnswer),
}


def _refusal(reason: str, status: int) -> Response:
    """A refusal, as the shell and the fleet CLI read one: the reason under ``detail``."""
    return error_response(reason, status)


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
            return _refusal("invalid browser name", HTTP_NOT_FOUND)
        try:
            return service.fleet.get(name)
        except UnknownBrowserError as e:
            return _refusal(str(e), HTTP_NOT_FOUND)

    def _start_browser(name: str | None, raw_url: str | None, is_launch_path: bool) -> Browser | Response:
        """The browser a nameless request means (the one that exists, started again if stopped, else a new one),
        or the named create, answered at once while Chromium launches in the background. A nameless request is the
        human's ``browser`` launch path (its start page held while an agent drives) or an agent's ``new`` (its page
        opened at once), as ``is_launch_path`` says."""
        missing = service.fleet.install_reason()
        if missing is not None:
            return _refusal(missing, HTTP_SERVICE_UNAVAILABLE)
        start_url: str | None = None
        if raw_url:
            try:
                start_url = str(AbsoluteHttpUrl(raw_url))
            except InvalidStartUrlError as e:
                return _refusal(f"url: {e}", HTTP_BAD_REQUEST)
        try:
            if name is None:
                return service.launch_browser(start_url) if is_launch_path else service.new_browser(start_url)
            service.ensure_started()
            return service.fleet.create(name, start_url)
        except InvalidBrowserNameError as e:
            return _refusal(str(e), HTTP_BAD_REQUEST)
        except (DuplicateBrowserNameError, FleetFullError) as e:
            return _refusal(str(e), HTTP_CONFLICT)
        except BrowserNotDrivableError as e:
            return _refusal(str(e), HTTP_CONFLICT)

    # the launch path and the message handler

    @blueprint.post(LAUNCH_BROWSER_PATH)
    def launch_browser() -> ResponseReturnValue:
        """The ``browser`` launch path: start the stack and Chromium when needed, open ``url`` as a tab in front when
        given (held while an agent drives), and answer the Screen's page."""
        body = parse_request_body(LaunchBrowserRequest)
        started = _start_browser(None, body.url, is_launch_path=True)
        if isinstance(started, Response):
            return started
        return model_response(LaunchAnswer(path=SCREEN_PAGE_PATH))

    @blueprint.post(OPEN_URL_PATH)
    def open_url() -> ResponseReturnValue:
        """A local page the human asked for: a tab in Chromium (held while an agent drives), and the Screen's page
        shown to the client that clicked."""
        body = parse_request_body(OpenUrlRequest)
        try:
            url = LocalHttpUrl(body.url)
        except InvalidStartUrlError as e:
            return _refusal(f"url: {e}", HTTP_BAD_REQUEST)
        try:
            client_id = ClientId(body.client_id)
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
        return model_response(
            OpenUrlAnswer(browser=str(browser.name), window_id=str(shown.window_id), shown=str(shown.shown))
        )

    # the fleet

    @blueprint.get("/browsers")
    def list_browsers() -> ResponseReturnValue:
        """Every browser of the fleet with its state, the lease and its tabs, and whether another can be created."""
        missing = service.fleet.install_reason()
        view = service.control.view()
        return model_response(
            BrowsersAnswer.model_validate(
                {
                    "browsers": [service.fleet.describe(browser, view) for browser in service.fleet.browsers()],
                    "can_create": missing is None,
                    "create_reason": missing or "",
                    "browser_count": service.fleet.launched_count(),
                    "browser_max": service.fleet.max_browsers,
                }
            )
        )

    @blueprint.post("/browsers")
    def create_browser() -> ResponseReturnValue:
        """Create a browser (``name`` and ``url`` optional) and answer its name at once; Chromium launches in the
        background. A nameless request answers the one browser, started again if it was stopped."""
        body = parse_request_body(CreateBrowserRequest)
        started = _start_browser(body.name, body.url, is_launch_path=False)
        if isinstance(started, Response):
            return started
        return model_response(BrowserNameAnswer(name=str(started.name)))

    @blueprint.delete("/browsers/<raw_name>")
    def close_browser(raw_name: str) -> ResponseReturnValue:
        """End the browser and forget it: its profile, with its logins, is deleted."""
        name = _browser_name(raw_name)
        if name is None:
            return _refusal("invalid browser name", HTTP_NOT_FOUND)
        service.fleet.close_and_forget(name)
        return model_response(ClosedAnswer(closed=True))

    @blueprint.post("/browsers/<raw_name>/stop")
    def stop_browser(raw_name: str) -> ResponseReturnValue:
        """End the browser's Chromium, keeping the browser, its profile and its tabs for a later start."""
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        try:
            service.fleet.stop_browser(browser.name)
        except BrowserNotDrivableError as e:
            return _refusal(str(e), HTTP_CONFLICT)
        return model_response(StoppedAnswer(stopped=True))

    @blueprint.post("/browsers/<raw_name>/start")
    def start_browser(raw_name: str) -> ResponseReturnValue:
        """Start a stopped browser again on its saved tabs, starting the stack first when it is down."""
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        missing = service.fleet.install_reason()
        if missing is not None:
            return _refusal(missing, HTTP_SERVICE_UNAVAILABLE)
        service.ensure_started()
        try:
            service.fleet.start_browser(browser.name)
        except FleetFullError as e:
            return _refusal(str(e), HTTP_CONFLICT)
        return model_response(StartedAnswer(started=True))

    @blueprint.post("/browsers/<raw_name>/close-tab")
    def close_active_tab(raw_name: str) -> ResponseReturnValue:
        """Close the browser's tab in front, keeping Chromium up."""
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        try:
            service.fleet.close_active_tab(browser.name)
        except BrowserNotDrivableError as e:
            return _refusal(str(e), HTTP_CONFLICT)
        return model_response(ClosedAnswer(closed=True))

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
            return _refusal(refusal, HTTP_FORBIDDEN)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        return model_response(CancelledAnswer(cancelled=service.fleet.cancel_pending_url(browser.name)))

    @blueprint.get("/browsers/<raw_name>/attach")
    def attach(raw_name: str) -> ResponseReturnValue:
        """An attach URL for the agent named by ``X-Mngr-Agent-Id`` to drive this browser through the CDP proxy, or
        why it cannot have one."""
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _refusal(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        return model_response(
            AttachAnswer.model_validate(service.fleet.attach_for(browser.name, agent_id, agent_name))
        )

    @blueprint.post("/browsers/<raw_name>/acquire")
    def acquire(raw_name: str) -> ResponseReturnValue:
        """The agent verb over a browser: acquire the Screen's lease (``reclaim`` to take it back); a browser that is
        not running answers the attach instead."""
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _refusal(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        body = parse_request_body(AcquireRequest)
        if not browser.is_running:
            return model_response(
                AttachAnswer.model_validate(service.fleet.attach_for(browser.name, agent_id, agent_name))
            )
        answer = service.acquire(agent_id, agent_name, is_reclaim=body.reclaim)
        if answer["ok"]:
            service.touch(agent_id)
        return model_response(
            BrowserAcquireAnswer.model_validate({**answer, "lifecycle": browser.lifecycle.value.lower()})
        )

    @blueprint.post("/browsers/<raw_name>/release")
    def release(raw_name: str) -> ResponseReturnValue:
        """The agent verb over a browser: release the Screen's lease."""
        agent_id, _ = _agent_identity()
        if agent_id is None:
            return _refusal(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        return model_response(ReleasedAnswer(released=service.release(agent_id)))

    @blueprint.post("/browsers/<raw_name>/handoff")
    def handoff(raw_name: str) -> ResponseReturnValue:
        """The agent verb over a browser: hand the Screen to the human, pinned, with a ``reason``."""
        agent_id, agent_name = _agent_identity()
        if agent_id is None:
            return _refusal(f"{AGENT_ID_HEADER} header required", HTTP_BAD_REQUEST)
        browser = _resolve(raw_name)
        if isinstance(browser, Response):
            return browser
        reason = parse_request_body(HandoffRequest).reason.strip() or "human verification needed"
        return model_response(HandoffAnswer.model_validate(service.handoff(agent_id, agent_name, reason)))

    return blueprint
