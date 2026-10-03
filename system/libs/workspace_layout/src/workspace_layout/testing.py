import json
import threading
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any
from typing import Final
from typing import Self

from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import LocationScope
from app_manifest.primitives import AppName
from app_manifest.primitives import AppUrl
from app_manifest.registry import RegistryLaunchPath
from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field
from pydantic import PrivateAttr

from workspace_layout.answers import ClientActivitySummary
from workspace_layout.answers import ClientView
from workspace_layout.answers import ContextAnswer
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import InventoryApp
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.errors import ShellOpError
from workspace_layout.errors import ShellRefusedOpError
from workspace_layout.interfaces import ShellLayoutInterface
from workspace_layout.ops import ClientActivityReport
from workspace_layout.ops import NavigateArgs
from workspace_layout.ops import OpenArgs
from workspace_layout.ops import PlaceArgs
from workspace_layout.ops import RefreshArgs
from workspace_layout.ops import RefreshWindowArgs
from workspace_layout.ops import ShowArgs
from workspace_layout.ops import WindowArgs
from workspace_layout.ops import parse_op_body
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import LayoutOp
from workspace_layout.primitives import ShowOutcome
from workspace_layout.primitives import WindowId
from workspace_layout.primitives import WindowPath
from workspace_layout.primitives import WindowTitle
from workspace_layout.records import DesktopLayoutView
from workspace_layout.records import DesktopShortcut
from workspace_layout.records import DesktopView
from workspace_layout.records import WindowView
from workspace_layout.shell_url import CLIENT_ACTIVITY_ROUTE
from workspace_layout.shell_url import LAYOUT_OP_ROUTE

# The window and desktop every answer of the fake shells names unless a test says otherwise.
FAKE_WINDOW_ID: Final[WindowId] = WindowId("win-0123456789abcdef")
FAKE_DESKTOP_ID: Final[DesktopId] = DesktopId("home")

# What the refusal of a client the fake shell does not know says, as the shell's 404 would.
_REFUSED_STATUS: Final[int] = 404

_SERVE_POLL_INTERVAL_SECONDS: Final[float] = 0.01


# When every client, window, and layout the fakes make was last seen, opened, or saved.
FAKE_TIME: Final[datetime] = datetime(2026, 9, 4, tzinfo=timezone.utc)


def connected_client(client_id: str) -> ClientView:
    """A client holding the socket, on no desktop yet."""
    return ClientView(id=ClientId(client_id), last_seen=FAKE_TIME, is_connected=True)


def fake_window(
    window_id: str, app: str, path: str, title: str = "", client_paths: Mapping[str, str] | None = None
) -> WindowView:
    """One window of a desktop as the shell's answers carry it: linked, or independent when it names each client's
    own path."""
    return WindowView(
        id=WindowId(window_id),
        app=AppName(app),
        path=WindowPath(path),
        title=WindowTitle(title),
        opened_at=FAKE_TIME,
        scope=LocationScope.LINKED if client_paths is None else LocationScope.INDEPENDENT,
        client_paths={ClientId(client): WindowPath(path) for client, path in (client_paths or {}).items()},
    )


def fake_desktop(
    desktop_id: str = FAKE_DESKTOP_ID,
    windows: Sequence[WindowView] = (),
    shortcuts: Sequence[DesktopShortcut] = (),
) -> DesktopView:
    """A desktop named for its id, with no wallpaper, as the shell's answers carry it."""
    return DesktopView(
        id=DesktopId(desktop_id),
        name=desktop_id.capitalize(),
        color="#4f46e5",
        glyph=0,
        wallpaper=None,
        shortcuts=tuple(shortcuts),
        windows=tuple(windows),
    )


def fake_app(
    name: str,
    launch_paths: Sequence[RegistryLaunchPath] = (),
    default_shortcut: DefaultShortcut | None = None,
    is_internal: bool = False,
) -> InventoryApp:
    """A running app of the inventory, named for itself, served on a loopback port nothing listens on."""
    return InventoryApp(
        name=AppName(name),
        display_name=name.capitalize(),
        icon="",
        label="",
        url=AppUrl(f"http://127.0.0.1:9/{name}"),
        internal=is_internal,
        program="",
        critical=False,
        stop_when_no_windows=False,
        launch_paths=tuple(launch_paths),
        default_shortcut=default_shortcut,
        launcher_rank=None,
        pin=None,
        message_handlers=(),
        is_running=True,
    )


def empty_layout() -> DesktopLayoutView:
    """A client's layout of a desktop it never placed anything on."""
    return DesktopLayoutView(version=1, updated_at=None, placements=(), window_paths={})


class FakeShell(ShellLayoutInterface):
    """An in-memory stand-in for the shell that records every request and answers it on ``window_id`` and
    ``desktop_id``: refused for a client in ``refused_client_ids`` or an op in ``refused_ops``, failing with
    ``error`` for every op while it is set, and with ``listing_error`` for the client and desktop lists."""

    model_config = {"extra": "forbid", "frozen": False, "arbitrary_types_allowed": True}

    clients: list[ClientView] = Field(default_factory=list, description="The connected clients the shell lists")
    desktop_list: list[DesktopView] = Field(
        default_factory=lambda: [fake_desktop()],
        description="The desktops the shell lists, the first being the fallback",
    )
    refused_client_ids: list[ClientId] = Field(default_factory=list, description="Clients every op for is refused")
    refused_ops: list[LayoutOp] = Field(default_factory=list, description="Ops that are refused whoever they are for")
    error: ShellOpError | None = Field(default=None, description="What every op raises while set")
    listing_error: ShellOpError | None = Field(
        default=None, description="What the client and desktop lists raise while set"
    )
    shown: ShowOutcome = Field(default=ShowOutcome.OPENED, description="How a show says it put its path on screen")
    window_id: WindowId = Field(default=FAKE_WINDOW_ID, description="The window every op answers")
    desktop_id: DesktopId = Field(default=FAKE_DESKTOP_ID, description="The desktop every op answers")
    shows: list[ShowArgs] = Field(default_factory=list, description="Every show asked for")
    opens: list[OpenArgs] = Field(default_factory=list, description="Every open asked for")
    focuses: list[WindowArgs] = Field(default_factory=list, description="Every focus asked for")
    navigations: list[NavigateArgs] = Field(default_factory=list, description="Every navigate asked for")
    placements: list[PlaceArgs] = Field(default_factory=list, description="Every place asked for")
    closes: list[WindowArgs] = Field(default_factory=list, description="Every close asked for")
    refreshes: list[RefreshArgs] = Field(default_factory=list, description="Every refresh asked for")
    activities: list[ClientActivityReport] = Field(default_factory=list, description="Every activity reported")

    def _check(self, op: LayoutOp, client_id: ClientId | None) -> None:
        if self.error is not None:
            raise self.error
        if op in self.refused_ops:
            raise ShellRefusedOpError(
                f"The shell refused the {op} ({_REFUSED_STATUS})", status_code=_REFUSED_STATUS, detail=""
            )
        if client_id is not None and client_id in self.refused_client_ids:
            detail = f"No client {client_id!r}"
            raise ShellRefusedOpError(
                f"The shell refused the {op} ({_REFUSED_STATUS}): {detail}", status_code=_REFUSED_STATUS, detail=detail
            )

    def _answer(self, client_id: ClientId | None) -> OpenAnswer:
        return OpenAnswer(
            desktop_id=self.desktop_id,
            client_id=client_id,
            desktop=fake_desktop(self.desktop_id),
            layout=empty_layout() if client_id is not None else None,
            window_id=self.window_id,
        )

    def show(self, args: ShowArgs) -> ShowAnswer:
        self.shows.append(args)
        self._check(LayoutOp.SHOW, args.client)
        return ShowAnswer.model_validate({**dict(self._answer(args.client)), "shown": self.shown})

    def open(self, args: OpenArgs) -> OpenAnswer:
        self.opens.append(args)
        self._check(LayoutOp.OPEN, args.client)
        return self._answer(args.client)

    def focus(self, args: WindowArgs) -> DesktopOpAnswer:
        self.focuses.append(args)
        self._check(LayoutOp.FOCUS, args.client)
        return self._answer(args.client)

    def navigate(self, args: NavigateArgs) -> DesktopOpAnswer:
        self.navigations.append(args)
        self._check(LayoutOp.NAVIGATE, args.client)
        return self._answer(args.client)

    def place(self, args: PlaceArgs) -> DesktopOpAnswer:
        self.placements.append(args)
        self._check(LayoutOp.PLACE, args.client)
        return self._answer(args.client)

    def close(self, args: WindowArgs) -> DesktopOpAnswer:
        self.closes.append(args)
        self._check(LayoutOp.CLOSE, args.client)
        return self._answer(args.client)

    def refresh(self, args: RefreshArgs) -> TransientOpAnswer:
        self.refreshes.append(args)
        client = args.client if isinstance(args, RefreshWindowArgs) else None
        self._check(LayoutOp.REFRESH, client)
        return TransientOpAnswer(target_client_id=client)

    def connected_clients(self) -> list[ClientView]:
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.clients)

    def desktops(self) -> list[DesktopView]:
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.desktop_list)

    def record_client_activity(self, report: ClientActivityReport) -> None:
        self.activities.append(report)


def describe_op_body_problem(body: Any) -> str | None:
    """Why the shell's op route would refuse ``body`` as it reads one (desktop contracts.md section 8), or None when
    it would take it."""
    try:
        parse_op_body(body)
    except InvalidLayoutValueError as e:
        return str(e)
    return None


def desktop_answer(desktop: DesktopView, client_id: str | None, window_id: str | None) -> dict[str, Any]:
    """What the shell answers a desktop op with (desktop contracts.md section 8), as the wire spells it."""
    answer = DesktopOpAnswer(
        desktop_id=desktop.id,
        client_id=None if client_id is None else ClientId(client_id),
        desktop=desktop,
        layout=None if client_id is None else empty_layout(),
        window_id=None if window_id is None else WindowId(window_id),
    )
    return answer.model_dump(mode="json")


def write_registry(path: Path, app_names: list[str]) -> Path:
    """A registry (``data/.state/apps.toml``) with one row per app name, as far as a reader of names needs it."""
    rows = "".join(f'[[apps]]\nname = "{name}"\nurl = "http://127.0.0.1:9/{name}"\n\n' for name in app_names)
    path.write_text(rows, encoding="utf-8")
    return path


class LoopbackShell(MutableModel):
    """A stand-in for the shell over loopback: its op route, its client-activity route, and whatever GET routes a
    test gives answers for, recording every body posted to it. Used as a context manager, it serves inside the
    ``with`` block and is closed on leaving it.

    Every op body is read as the shell reads one (``describe_op_body_problem``), so a caller that posts a body the
    shell would refuse is refused here too, with a 400 whose detail says why.
    """

    model_config = {"extra": "forbid", "frozen": False, "arbitrary_types_allowed": True}

    get_answers: dict[str, tuple[int, Any]] = Field(
        default_factory=dict,
        description="What each GET route answers, as (status, body): a dict or list is JSON, a str sent as it is",
    )
    op_answer: dict[str, Any] = Field(
        default_factory=lambda: desktop_answer(fake_desktop(), "c1", None),
        description="What a document op answers",
    )
    op_refusal: tuple[int, Any] | None = Field(
        default=None, description="A (status, body) every document op is answered with instead, while set"
    )
    context_clients: list[ClientActivitySummary] = Field(default_factory=list, description="What ``context`` lists")
    refresh_target: ClientId | None = Field(
        default=ClientId("c1"), description="The client a ``refresh`` says it reached"
    )
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
        op = parse_op_body(body).op
        if op is LayoutOp.CONTEXT:
            return 200, ContextAnswer(clients=tuple(self.context_clients)).model_dump(mode="json")
        if op is LayoutOp.REFRESH:
            return 200, TransientOpAnswer(target_client_id=self.refresh_target).model_dump(mode="json")
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

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
