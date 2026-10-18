"""Shared helpers for the Screen's tests."""

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import Server
from websockets.sync.server import ServerConnection
from websockets.sync.server import serve

from imbue.imbue_common.mutable_model import MutableModel
from screen.data_types import Lifecycle
from screen.errors import SupervisorError
from screen.interfaces import ProgramState
from screen.interfaces import SupervisorInterface
from screen.neko_config import PULSE_SOCKET_PATH
from screen.primitives import DISPLAY
from screen.primitives import NEKO_PROGRAM
from screen.primitives import PULSE_PROGRAM
from screen.primitives import RUNTIME_DIR
from screen.primitives import SESSION_PROGRAM
from screen.primitives import STACK_PROGRAMS
from screen.primitives import XORG_PROGRAM
from screen.service import ScreenService
from screen.stack import NEKO_BINARY_PATH
from screen.stack import NEKO_YAML_FILENAME
from screen.stack import PULSE_SCRIPT_FILENAME
from screen.stack import XORG_CONF_FILENAME

_DEFAULT_WAIT_SECONDS: Final[float] = 10.0
_POLL_SECONDS: Final[float] = 0.02


class ManualClock:
    """A monotonic clock a test advances by hand, standing in for ``time.monotonic``."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def wait_until(predicate: Callable[[], bool], timeout_seconds: float = _DEFAULT_WAIT_SECONDS) -> bool:
    """Poll ``predicate`` until it holds or the deadline passes."""
    deadline = time.monotonic() + timeout_seconds
    pause = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        pause.wait(_POLL_SECONDS)
    return predicate()


def start_service_stack(service: ScreenService) -> None:
    """Start the stack through its on-demand path and wait until it runs."""
    service.ensure_started()
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING), service.stack.stop_reason


class FakeChromiumServer(MutableModel):
    """A stand-in for Chromium's debug endpoint: ``/json/version`` and ``/json/list`` over HTTP, and a browser
    WebSocket that answers the target domain's calls over a tab list in memory (``Target.getTargets`` in reverse
    order, as Chromium's arbitrary order would have it; ``Target.setDiscoverTargets`` replays ``targetCreated`` in
    creation order), records every frame, and echoes anything else with an empty result."""

    model_config = {"arbitrary_types_allowed": True}

    targets: list[dict[str, Any]] = Field(default_factory=list, description="The targets, in creation order")
    frames: list[dict[str, Any]] = Field(default_factory=list, description="Every frame received, in order")
    unanswered_methods: set[str] = Field(default_factory=set, description="Methods whose reply is never sent")
    _http: ThreadingHTTPServer | None = PrivateAttr(default=None)
    _socket_server: Server | None = PrivateAttr(default=None)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    @property
    def http_endpoint(self) -> str:
        assert self._http is not None
        return f"http://127.0.0.1:{self._http.server_address[1]}"

    @property
    def websocket_url(self) -> str:
        assert self._socket_server is not None
        return f"ws://127.0.0.1:{self._socket_server.socket.getsockname()[1]}/devtools/browser/fake"

    def __enter__(self) -> "FakeChromiumServer":
        self._socket_server = serve(self._handle, "127.0.0.1", 0, max_size=None)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/json/version"):
                    body = json.dumps({"Browser": "Chrome/153", "webSocketDebuggerUrl": server.websocket_url})
                elif self.path.rstrip("/").endswith("/json/list"):
                    body = json.dumps(
                        [
                            {**target, "webSocketDebuggerUrl": server.websocket_url, "devtoolsFrontendUrl": "/x"}
                            for target in server.targets
                        ]
                    )
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                encoded = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._threads = [
            threading.Thread(target=self._socket_server.serve_forever, daemon=True),
            threading.Thread(target=self._http.serve_forever, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
        if self._socket_server is not None:
            self._socket_server.shutdown()
        for thread in self._threads:
            thread.join(timeout=5)

    def _handle(self, connection: ServerConnection) -> None:
        try:
            for raw in connection:
                frame = json.loads(raw)
                with self._lock:
                    self.frames.append(frame)
                for reply in self._answer(frame):
                    connection.send(json.dumps(reply))
        except ConnectionClosed:
            pass

    def _answer(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        method = frame.get("method")
        params = frame.get("params") or {}
        if method in self.unanswered_methods:
            return []
        if method == "Target.setDiscoverTargets":
            events = [
                {"method": "Target.targetCreated", "params": {"targetInfo": dict(target)}} for target in self.targets
            ]
            return [*events, {"id": frame["id"], "result": {}}]
        if method == "Target.getTargets":
            return [
                {"id": frame["id"], "result": {"targetInfos": [dict(target) for target in reversed(self.targets)]}}
            ]
        if method == "Target.createTarget":
            target = {"targetId": f"t{len(self.targets) + 1}", "type": "page", "url": params["url"], "title": ""}
            self.targets.append(target)
            return [
                {"method": "Target.targetCreated", "params": {"targetInfo": dict(target)}},
                {"id": frame["id"], "result": {"targetId": target["targetId"]}},
            ]
        if method == "Target.closeTarget":
            self.targets = [target for target in self.targets if target["targetId"] != params["targetId"]]
            return [
                {"method": "Target.targetDestroyed", "params": {"targetId": params["targetId"]}},
                {"id": frame["id"], "result": {"success": True}},
            ]
        if method == "Target.attachToTarget":
            return [{"id": frame["id"], "result": {"sessionId": f"session-{params['targetId']}"}}]
        if method == "Runtime.evaluate":
            is_first = frame.get("sessionId") == f"session-{self.targets[0]['targetId']}" if self.targets else False
            return [{"id": frame["id"], "result": {"result": {"type": "boolean", "value": is_first}}}]
        if method == "Browser.getVersion":
            return [{"id": frame["id"], "result": {"product": "Chrome/153"}}]
        if method == "Browser.close":
            return [{"id": frame["id"], "error": {"code": -32000, "message": "never reached"}}]
        return [{"id": frame["id"], "result": {}}]


class SubprocessSupervisor(SupervisorInterface):
    """The group's programs as plain subprocesses with the drop-ins' commands, for a test that runs the real stack
    where no supervisord does (the workspace image's CI run)."""

    model_config = {"arbitrary_types_allowed": True}

    state_dir: Path = Field(frozen=True, description="Where the rendered configuration is")
    log_dir: Path = Field(frozen=True, description="Where each program's output goes")
    processes: dict[str, subprocess.Popen[bytes]] = Field(default_factory=dict, description="Live processes")

    def _command(self, program: str) -> list[str]:
        if program == XORG_PROGRAM:
            return [
                "Xorg",
                DISPLAY,
                "-config",
                str(self.state_dir / XORG_CONF_FILENAME),
                "-noreset",
                "-nolisten",
                "tcp",
                "-novtswitch",
                "-logfile",
                str(self.log_dir / "Xorg.log"),
            ]
        if program == PULSE_PROGRAM:
            return [
                "pulseaudio",
                "--daemonize=no",
                "--exit-idle-time=-1",
                "--disallow-exit",
                "--disallow-module-loading",
                "-nF",
                str(self.state_dir / PULSE_SCRIPT_FILENAME),
            ]
        if program == SESSION_PROGRAM:
            return ["startxfce4"]
        if program == NEKO_PROGRAM:
            return [str(NEKO_BINARY_PATH), "serve", "--config", str(self.state_dir / NEKO_YAML_FILENAME)]
        raise SupervisorError(f"no command for {program}")

    def start_program(self, program: str) -> None:
        if program in self.processes and self.processes[program].poll() is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        environment = {
            **os.environ,
            "DISPLAY": DISPLAY,
            "XDG_RUNTIME_DIR": RUNTIME_DIR,
            "PULSE_SERVER": f"unix:{PULSE_SOCKET_PATH}",
        }
        with (self.log_dir / f"{program}.log").open("ab") as log:
            self.processes[program] = subprocess.Popen(
                self._command(program), env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )

    def stop_program(self, program: str) -> None:
        process = self.processes.get(program)
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    def program_state(self, program: str) -> ProgramState:
        process = self.processes.get(program)
        if process is None:
            return ProgramState.STOPPED
        if process.poll() is None:
            return ProgramState.RUNNING
        return ProgramState.EXITED if process.returncode != 0 or program in self.processes else ProgramState.STOPPED

    def program_pid(self, program: str) -> int | None:
        process = self.processes.get(program)
        if process is None or process.poll() is not None:
            return None
        return process.pid

    def kill_program(self, program: str) -> None:
        """End a program the way memory pressure would."""
        process = self.processes[program]
        process.kill()
        process.wait(timeout=10)

    def stop_everything(self) -> None:
        for program in reversed(STACK_PROGRAMS):
            self.stop_program(program)
