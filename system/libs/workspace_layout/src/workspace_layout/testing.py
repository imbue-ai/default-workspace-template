import json
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Final

from app_manifest.manifest import describe_validation_error
from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from workspace_layout.answers import ConnectedClient
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import DesktopSummary
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import ShowAnswer
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.errors import ShellOpError
from workspace_layout.errors import ShellRefusedOpError
from workspace_layout.interfaces import ShellLayoutInterface
from workspace_layout.ops import CLOSE_OP
from workspace_layout.ops import CONTEXT_OP
from workspace_layout.ops import FOCUS_OP
from workspace_layout.ops import INVENTORY_OPS
from workspace_layout.ops import NAVIGATE_OP
from workspace_layout.ops import OPEN_OP
from workspace_layout.ops import PLACE_OP
from workspace_layout.ops import REFRESH_OP
from workspace_layout.ops import SHOW_OP
from workspace_layout.ops import TARGET_ARG_KEYS
from workspace_layout.ops import ClientActivityReport
from workspace_layout.ops import DesktopOpArguments
from workspace_layout.ops import NavigateRequest
from workspace_layout.ops import OpenRequest
from workspace_layout.ops import PlaceRequest
from workspace_layout.ops import ShowRequest
from workspace_layout.ops import WindowRequest
from workspace_layout.ops import is_known_op
from workspace_layout.ops import op_only_args
from workspace_layout.ops import parse_op_requester
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import WindowId
from workspace_layout.shell_url import CLIENT_ACTIVITY_ROUTE
from workspace_layout.shell_url import LAYOUT_OP_ROUTE

# The window and desktop every answer of the fake shells names unless a test says otherwise.
FAKE_WINDOW_ID: Final[WindowId] = WindowId("win-0123456789abcdef")
FAKE_DESKTOP_ID: Final[DesktopId] = DesktopId("home")

# What the refusal of a client the fake shell does not know says, as the shell's 404 would.
_REFUSED_STATUS: Final[int] = 404

_SERVE_POLL_INTERVAL_SECONDS: Final[float] = 0.01


def connected_client(client_id: str) -> ConnectedClient:
    """A client holding the socket, on no desktop yet."""
    return ConnectedClient(id=ClientId(client_id), active_desktop=None, is_connected=True)


class FakeShell(ShellLayoutInterface):
    """An in-memory stand-in for the shell that records every request and answers it on ``window_id`` and
    ``desktop_id``: refused for a client in ``refused_client_ids`` or an op in ``refused_ops``, failing with
    ``error`` for every op while it is set, and with ``listing_error`` for the client and desktop lists."""

    model_config = {"extra": "forbid", "frozen": False, "arbitrary_types_allowed": True}

    clients: list[ConnectedClient] = Field(default_factory=list, description="The connected clients the shell lists")
    desktop_list: list[DesktopSummary] = Field(
        default_factory=lambda: [DesktopSummary(id=FAKE_DESKTOP_ID, name="Home")],
        description="The desktops the shell lists, the first being the fallback",
    )
    refused_client_ids: list[ClientId] = Field(default_factory=list, description="Clients every op for is refused")
    refused_ops: list[str] = Field(default_factory=list, description="Ops that are refused whoever they are for")
    error: ShellOpError | None = Field(default=None, description="What every op raises while set")
    listing_error: ShellOpError | None = Field(
        default=None, description="What the client and desktop lists raise while set"
    )
    shown: str = Field(default="opened", description="How a show says it put its path on screen")
    window_id: WindowId = Field(default=FAKE_WINDOW_ID, description="The window every op answers")
    desktop_id: DesktopId = Field(default=FAKE_DESKTOP_ID, description="The desktop every op answers")
    shows: list[ShowRequest] = Field(default_factory=list, description="Every show asked for")
    opens: list[OpenRequest] = Field(default_factory=list, description="Every open asked for")
    focuses: list[WindowRequest] = Field(default_factory=list, description="Every focus asked for")
    navigations: list[NavigateRequest] = Field(default_factory=list, description="Every navigate asked for")
    placements: list[PlaceRequest] = Field(default_factory=list, description="Every place asked for")
    closes: list[WindowRequest] = Field(default_factory=list, description="Every close asked for")
    refreshes: list[WindowRequest] = Field(default_factory=list, description="Every refresh asked for")
    activities: list[ClientActivityReport] = Field(default_factory=list, description="Every activity reported")

    def _check(self, op: str, client_id: ClientId | None) -> None:
        if self.error is not None:
            raise self.error
        if op in self.refused_ops:
            raise ShellRefusedOpError(f"The shell refused the {op} ({_REFUSED_STATUS})", status_code=_REFUSED_STATUS)
        if client_id is not None and client_id in self.refused_client_ids:
            raise ShellRefusedOpError(
                f"The shell refused the {op} ({_REFUSED_STATUS}): No client {client_id!r}",
                status_code=_REFUSED_STATUS,
            )

    def _answer(self, client_id: ClientId | None) -> DesktopOpAnswer:
        return DesktopOpAnswer(desktop_id=self.desktop_id, client_id=client_id, window_id=self.window_id)

    def show(self, request: ShowRequest) -> ShowAnswer:
        self.shows.append(request)
        self._check(SHOW_OP, request.client_id)
        return ShowAnswer(
            desktop_id=self.desktop_id, client_id=request.client_id, window_id=self.window_id, shown=self.shown
        )

    def open(self, request: OpenRequest) -> OpenAnswer:
        self.opens.append(request)
        self._check(OPEN_OP, request.client_id)
        return OpenAnswer(desktop_id=self.desktop_id, client_id=request.client_id, window_id=self.window_id)

    def focus(self, request: WindowRequest) -> DesktopOpAnswer:
        self.focuses.append(request)
        self._check(FOCUS_OP, request.client_id)
        return self._answer(request.client_id)

    def navigate(self, request: NavigateRequest) -> DesktopOpAnswer:
        self.navigations.append(request)
        self._check(NAVIGATE_OP, request.client_id)
        return self._answer(request.client_id)

    def place(self, request: PlaceRequest) -> DesktopOpAnswer:
        self.placements.append(request)
        self._check(PLACE_OP, request.client_id)
        return self._answer(request.client_id)

    def close(self, request: WindowRequest) -> DesktopOpAnswer:
        self.closes.append(request)
        self._check(CLOSE_OP, request.client_id)
        return self._answer(request.client_id)

    def refresh(self, request: WindowRequest) -> None:
        self.refreshes.append(request)
        self._check(REFRESH_OP, request.client_id)

    def connected_clients(self) -> list[ConnectedClient]:
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.clients)

    def desktops(self) -> list[DesktopSummary]:
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.desktop_list)

    def record_client_activity(self, report: ClientActivityReport) -> None:
        self.activities.append(report)


def describe_op_body_problem(body: Any) -> str | None:
    """Why the shell's op route would refuse ``body`` as it reads one (desktop contracts.md section 8), or None when
    it would take it: the op a known one, the requester an ``{app, marker}`` or nothing, and the arguments ones the
    shared ``DesktopOpArguments`` model accepts."""
    if not isinstance(body, dict):
        return "the body is not a JSON object"
    op = body.get("op")
    if not isinstance(op, str) or not is_known_op(op):
        return f"unknown op {op!r}"
    try:
        parse_op_requester(body.get("requester"))
    except InvalidLayoutValueError as e:
        return str(e)
    arguments = body.get("args", {})
    if not isinstance(arguments, dict):
        return "``args`` is not a JSON object"
    if op == CONTEXT_OP or op in INVENTORY_OPS:
        return None
    for key in TARGET_ARG_KEYS & arguments.keys():
        if not isinstance(arguments[key], str):
            return f"``args.{key}`` is not a string"
    try:
        DesktopOpArguments.model_validate(op_only_args(arguments))
    except ValidationError as e:
        return describe_validation_error(e)
    return None


def desktop_answer(
    desktop_id: str,
    client_id: str | None,
    windows: list[dict[str, Any]],
    window_id: str | None,
    shortcuts: list[dict[str, Any]],
) -> dict[str, Any]:
    """What the shell answers a desktop op with (desktop contracts.md section 8), as the wire spells it."""
    return {
        "ok": True,
        "desktop_id": desktop_id,
        "client_id": client_id,
        "desktop": {
            "id": desktop_id,
            "name": desktop_id.capitalize(),
            "wallpaper": None,
            "shortcuts": shortcuts,
            "windows": windows,
        },
        "layout": {"version": 1, "updated_at": None, "placements": []},
        "window_id": window_id,
    }


def window_json(window_id: str, app: str, path: str, title: str) -> dict[str, Any]:
    """One window of a desktop, as the wire spells it."""
    return {"id": window_id, "app": app, "path": path, "title": title, "opened_at": "2026-09-04T00:00:00+00:00"}


def write_registry(path: Path, app_names: list[str]) -> Path:
    """A registry (``data/.state/apps.toml``) with one row per app name, as far as a reader of names needs it."""
    rows = "".join(f'[[apps]]\nname = "{name}"\nurl = "http://127.0.0.1:9/{name}"\n\n' for name in app_names)
    path.write_text(rows, encoding="utf-8")
    return path


class LoopbackShell(MutableModel):
    """A stand-in for the shell over loopback: its op route, its client-activity route, and whatever GET routes a
    test gives answers for, recording every body posted to it.

    Every op body is read as the shell reads one (``describe_op_body_problem``), so a caller that posts a body the
    shell would refuse is refused here too, with a 400 whose detail says why.
    """

    model_config = {"extra": "forbid", "frozen": False, "arbitrary_types_allowed": True}

    get_answers: dict[str, tuple[int, Any]] = Field(
        default_factory=dict,
        description="What each GET route answers, as (status, body): a dict or list is JSON, a str sent as it is",
    )
    op_answer: dict[str, Any] = Field(
        default_factory=lambda: desktop_answer("home", "c1", [], None, []),
        description="What a document op answers",
    )
    op_refusal: tuple[int, Any] | None = Field(
        default=None, description="A (status, body) every document op is answered with instead, while set"
    )
    context_clients: list[dict[str, Any]] = Field(default_factory=list, description="What ``context`` lists")
    refresh_target: str | None = Field(default="c1", description="The client a ``refresh`` says it reached")
    activity_status: int = Field(default=204, description="What the client-activity route answers")
    answer_headers: dict[str, str] = Field(
        default_factory=dict, description="Headers every answer carries besides its Content-Type and Content-Length"
    )
    posted: list[tuple[str, Any]] = Field(default_factory=list, description="Every (route, body) posted, in order")
    posted_content_types: list[str | None] = Field(
        default_factory=list, description="The Content-Type of every post, in order"
    )
    _server: ThreadingHTTPServer | None = PrivateAttr(default=None)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def posted_ops(self) -> list[tuple[str, dict[str, Any]]]:
        """Every op posted to the op route, as (op, args)."""
        return [(body["op"], body["args"]) for route, body in self.posted if route == LAYOUT_OP_ROUTE]

    def answer_op(self, body: Any) -> tuple[int, Any]:
        problem = describe_op_body_problem(body)
        if problem is not None:
            return 400, {"detail": problem}
        op = body["op"]
        if op == CONTEXT_OP:
            return 200, {"ok": True, "clients": self.context_clients}
        if op == REFRESH_OP:
            return 200, {"ok": True, "target_client_id": self.refresh_target}
        if self.op_refusal is not None:
            return self.op_refusal
        return 200, self.op_answer

    def start(self) -> None:
        shell = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _respond(self, status: int, body: Any) -> None:
                is_text = isinstance(body, str)
                payload = (body if is_text else json.dumps(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html" if is_text else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in shell.answer_headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                answer = shell.get_answers.get(self.path)
                if answer is None:
                    self._respond(404, {"detail": f"No such API route: {self.path}"})
                    return
                self._respond(*answer)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"null")
                shell.posted.append((self.path, body))
                shell.posted_content_types.append(self.headers.get("Content-Type"))
                if self.path == LAYOUT_OP_ROUTE:
                    self._respond(*shell.answer_op(body))
                elif self.path == CLIENT_ACTIVITY_ROUTE:
                    self._respond(shell.activity_status, "" if shell.activity_status == 204 else {"detail": "refused"})
                else:
                    self._respond(404, {"detail": f"No such API route: {self.path}"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # A short poll keeps ``close`` (which waits out one poll) from dominating a test.
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": _SERVE_POLL_INTERVAL_SECONDS}, daemon=True
        )
        thread.start()
        self._server = server
        self._thread = thread

    @property
    def url(self) -> str:
        if self._server is None:
            raise ShellOpError("the loopback shell is not serving")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        """Stop serving; the URL then refuses connections, as a shell that is down does."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
