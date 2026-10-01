import http.client
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from http.client import HTTPMessage
from typing import IO
from typing import Any
from typing import Final

from app_manifest.primitives import AppName
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import Field

from workspace_layout.answers import ClientView
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.answers import parse_answer
from workspace_layout.answers import parse_listing
from workspace_layout.answers import quote_answer
from workspace_layout.errors import ShellRefusedOpError
from workspace_layout.errors import ShellUnreachableError
from workspace_layout.interfaces import ShellLayoutInterface
from workspace_layout.ops import ClientActivityReport
from workspace_layout.ops import NavigateRequest
from workspace_layout.ops import OpenRequest
from workspace_layout.ops import OpRequester
from workspace_layout.ops import PlaceRequest
from workspace_layout.ops import ShowRequest
from workspace_layout.ops import WindowRequest
from workspace_layout.ops import navigate_op_arguments
from workspace_layout.ops import op_request_body
from workspace_layout.ops import open_op_arguments
from workspace_layout.ops import place_op_arguments
from workspace_layout.ops import show_op_arguments
from workspace_layout.ops import window_op_arguments
from workspace_layout.primitives import LayoutOp
from workspace_layout.records import DesktopView
from workspace_layout.shell_url import CLIENT_ACTIVITY_ROUTE
from workspace_layout.shell_url import CLIENTS_ROUTE
from workspace_layout.shell_url import DESKTOPS_ROUTE
from workspace_layout.shell_url import LAYOUT_OP_ROUTE

ENV_MINDS_CHAT_ID: Final[str] = "MINDS_CHAT_ID"
ENV_MNGR_AGENT_ID: Final[str] = "MNGR_AGENT_ID"
# The requester an agent's op carries is its own chat: the chat app's name, and the chat id its window's path carries.
AGENT_REQUESTER_APP: Final[AppName] = AppName("chat")

# The shell answers every one of these from memory and its state files; past this a request is suspicious.
SHELL_REQUEST_SLOW_SECONDS: Final[float] = 0.5

_HTTP_SUCCESS_RANGE: Final[range] = range(200, 300)


class ShellResponse(FrozenModel):
    """One answer of the shell as it came: the status, and the body as a JSON object or else its text."""

    status_code: int = Field(description="The HTTP status")
    body: dict[str, Any] | str = Field(description="The body parsed as a JSON object, else its text")

    @property
    def is_success(self) -> bool:
        return self.status_code in _HTTP_SUCCESS_RANGE


@pure
def _json_object_or_text(text: str) -> dict[str, Any] | str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return parsed if isinstance(parsed, dict) else text


class _RedirectsAsAnswersHandler(urllib.request.HTTPRedirectHandler):
    """Declines every redirect, so a 3xx reaches the caller as an HTTPError carrying the shell's own answer."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


_SHELL_OPENER: Final[urllib.request.OpenerDirector] = urllib.request.build_opener(_RedirectsAsAnswersHandler)


def _exchange(request: urllib.request.Request, timeout_seconds: float) -> tuple[int, bytes]:
    try:
        with _SHELL_OPENER.open(request, timeout=timeout_seconds) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()


def request_shell(method: str, url: str, body: Mapping[str, Any] | None, timeout_seconds: float) -> ShellResponse:
    """One request to a shell route, answered as it came; raises ShellUnreachableError when it could not be made
    (refused, timed out, or cut off)."""
    # The stdlib rather than httpx: the workspace-layout command starts once per agent action, and importing httpx
    # (which loads rich and pygments for its own CLI) would be a large share of that startup.
    data = None if body is None else json.dumps(dict(body)).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
    except ValueError as e:
        raise ShellUnreachableError(str(e)) from e
    try:
        status_code, raw = _exchange(request, timeout_seconds)
    except (OSError, http.client.HTTPException) as e:
        raise ShellUnreachableError(str(e) or type(e).__name__) from e
    return ShellResponse(status_code=status_code, body=_json_object_or_text(raw.decode("utf-8", errors="replace")))


def requester_from_environment() -> OpRequester | None:
    """The calling agent's own chat as an op's requester, or None outside an agent: ``MINDS_CHAT_ID`` from the chat
    app that created the agent, else the agent's own id (an agent created any other way is its own chat)."""
    chat_id = os.environ.get(ENV_MINDS_CHAT_ID, "") or os.environ.get(ENV_MNGR_AGENT_ID, "")
    if not chat_id:
        return None
    return OpRequester(app=AGENT_REQUESTER_APP, marker=chat_id)


class ShellLayoutClient(ShellLayoutInterface):
    """The shell over loopback: its client list, its desktops, its client-activity log, and its op route."""

    shell_url: str = Field(frozen=True, description="The shell's base URL, without a trailing slash")
    requester: OpRequester | None = Field(frozen=True, description="Who every op says asked; None for nobody")
    timeout_seconds: float = Field(frozen=True, description="How long one request may take before it is abandoned")

    def _request(self, method: str, route: str, body: Mapping[str, Any] | None, described: str) -> ShellResponse:
        url = f"{self.shell_url}{route}"
        started_at = time.monotonic()
        try:
            response = request_shell(method, url, body, self.timeout_seconds)
        except ShellUnreachableError as e:
            raise ShellUnreachableError(f"Could not reach the shell at {url} for the {described}: {e}") from e
        elapsed = time.monotonic() - started_at
        if elapsed > SHELL_REQUEST_SLOW_SECONDS:
            logger.warning("Asked the shell for the {} slowly, in {:.1f}s", described, elapsed)
        if not response.is_success:
            raise ShellRefusedOpError(
                f"The shell refused the {described} ({response.status_code}): {quote_answer(response.body)}",
                status_code=response.status_code,
            )
        return response

    def _post_op(self, op: LayoutOp, arguments: Mapping[str, Any]) -> dict[str, Any] | str:
        return self._request("POST", LAYOUT_OP_ROUTE, op_request_body(op, arguments, self.requester), op).body

    def show(self, request: ShowRequest) -> ShowAnswer:
        return parse_answer(ShowAnswer, self._post_op(LayoutOp.SHOW, show_op_arguments(request)), LayoutOp.SHOW)

    def open(self, request: OpenRequest) -> OpenAnswer:
        return parse_answer(OpenAnswer, self._post_op(LayoutOp.OPEN, open_op_arguments(request)), LayoutOp.OPEN)

    def focus(self, request: WindowRequest) -> DesktopOpAnswer:
        return parse_answer(
            DesktopOpAnswer, self._post_op(LayoutOp.FOCUS, window_op_arguments(request)), LayoutOp.FOCUS
        )

    def navigate(self, request: NavigateRequest) -> DesktopOpAnswer:
        return parse_answer(
            DesktopOpAnswer, self._post_op(LayoutOp.NAVIGATE, navigate_op_arguments(request)), LayoutOp.NAVIGATE
        )

    def place(self, request: PlaceRequest) -> DesktopOpAnswer:
        return parse_answer(
            DesktopOpAnswer, self._post_op(LayoutOp.PLACE, place_op_arguments(request)), LayoutOp.PLACE
        )

    def close(self, request: WindowRequest) -> DesktopOpAnswer:
        return parse_answer(
            DesktopOpAnswer, self._post_op(LayoutOp.CLOSE, window_op_arguments(request)), LayoutOp.CLOSE
        )

    def refresh(self, request: WindowRequest) -> TransientOpAnswer:
        return parse_answer(
            TransientOpAnswer, self._post_op(LayoutOp.REFRESH, window_op_arguments(request)), LayoutOp.REFRESH
        )

    def connected_clients(self) -> list[ClientView]:
        body = self._request("GET", CLIENTS_ROUTE, None, "client list").body
        return [client for client in parse_listing(ClientView, body, "clients", "client list") if client.is_connected]

    def desktops(self) -> list[DesktopView]:
        body = self._request("GET", DESKTOPS_ROUTE, None, "desktops").body
        return parse_listing(DesktopView, body, "desktops", "desktops")

    def record_client_activity(self, report: ClientActivityReport) -> None:
        # The shell may be down or restarting, and the caller keeps working without it, so an unreachable or failing
        # shell is a debug log; one that refuses the body is a warning, since the two apps disagree about its shape.
        try:
            self._request("POST", CLIENT_ACTIVITY_ROUTE, report.model_dump(mode="json"), "client-activity report")
        except ShellUnreachableError as e:
            logger.debug("Skipped reporting client activity: {}", e)
        except ShellRefusedOpError as e:
            if e.status_code < 500:
                logger.warning("Reported client activity and the shell refused it: {}", e)
            else:
                logger.debug("Reported client activity and the shell failed: {}", e)


@pure
def _no_shell_error(described: str) -> ShellUnreachableError:
    return ShellUnreachableError(f"No shell is connected to {described}")


class DisconnectedShell(ShellLayoutInterface):
    """A shell with nobody connected and nothing to show on: the stand-in where no shell is wired (a test, a
    secondary chat)."""

    def show(self, request: ShowRequest) -> ShowAnswer:
        raise _no_shell_error(f"show {request.path} to client {request.client_id}")

    def open(self, request: OpenRequest) -> OpenAnswer:
        raise _no_shell_error(f"open {request.path} of {request.app}")

    def focus(self, request: WindowRequest) -> DesktopOpAnswer:
        raise _no_shell_error(f"focus window {request.window}")

    def navigate(self, request: NavigateRequest) -> DesktopOpAnswer:
        raise _no_shell_error(f"navigate window {request.window}")

    def place(self, request: PlaceRequest) -> DesktopOpAnswer:
        raise _no_shell_error(f"place window {request.window}")

    def close(self, request: WindowRequest) -> DesktopOpAnswer:
        raise _no_shell_error(f"close window {request.window}")

    def refresh(self, request: WindowRequest) -> TransientOpAnswer:
        raise _no_shell_error(f"refresh window {request.window}")

    def connected_clients(self) -> list[ClientView]:
        return []

    def desktops(self) -> list[DesktopView]:
        return []

    def record_client_activity(self, report: ClientActivityReport) -> None:
        return None
