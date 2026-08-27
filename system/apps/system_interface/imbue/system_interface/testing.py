"""Shared test fakes for the system_interface package.

Houses deterministic stand-ins for outside-world dependencies that
`ClaudeAuthService` takes as constructor-injected callables
(`command_runner`, `pexpect_spawner`). Both `claude_auth_test.py` and
`harnesses/claude/auth_endpoints_test.py` need the same fakes, so they live here
rather than being copy-pasted into each test module.

Also houses `build_test_state`, the test-side composition root: it builds a
`SystemInterfaceState` with fakes for whichever collaborators a test overrides
and cheap real instances for the rest, mirroring `main.build_production_state`
without ever starting the agent manager.
"""

from __future__ import annotations

import fcntl
import os
import socket
import socketserver
import sys
import threading
import time
import xmlrpc.client
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from xmlrpc.server import SimpleXMLRPCDispatcher
from xmlrpc.server import SimpleXMLRPCRequestHandler

import httpx
import pexpect
import simple_websocket
from flask import Flask

from imbue.mngr.api.find import AgentMatch
from imbue.mngr.primitives import AgentId
from imbue.system_interface.agent_discovery import MngrMessenger
from imbue.system_interface.agent_discovery import SendFailure
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.config import Config
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.harnesses.auth_flows import AuthFlowService
from imbue.system_interface.harnesses.claude.auth import ClaudeAuthService
from imbue.system_interface.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.system_interface.layout_ops import LayoutMutex
from imbue.system_interface.welcome_resend import WelcomeResender
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server

# The workspace's browser engine is Fortress (a stealth-patched Chromium fork)
# provisioned by env-converge. Playwright's own browser-cache lookup only
# auto-discovers builds Playwright downloaded itself, so launches must name
# this binary explicitly via ``executable_path`` (see the
# ``browser_type_launch_args`` fixture override in ``conftest.py``).
FORTRESS_CHROMIUM_PATH = Path("/opt/fortress/tilion-fortress/tilion")


class _FakeSupervisorRequestHandler(SimpleXMLRPCRequestHandler):
    """XML-RPC request handler usable over a unix socket."""

    # TCP_NODELAY is meaningless (and an error) on a unix socket.
    disable_nagle_algorithm = False

    def address_string(self) -> str:
        # A unix socket has no peer address; the base implementation indexes
        # into an empty client_address and dies mid-request.
        return "unix-socket"


class _UnixSocketXmlRpcServer(socketserver.ThreadingUnixStreamServer, SimpleXMLRPCDispatcher):
    """A minimal XML-RPC server over a unix socket."""

    # Read by SimpleXMLRPCRequestHandler on every request.
    logRequests = False

    def __init__(self, socket_path: str) -> None:
        SimpleXMLRPCDispatcher.__init__(self, allow_none=False, encoding=None)
        socketserver.ThreadingUnixStreamServer.__init__(self, socket_path, _FakeSupervisorRequestHandler)


# supervisord's own fault codes (supervisor.xmlrpc.Faults), restated for the fake.
_SUPERVISOR_FAULT_BAD_NAME = 10
_SUPERVISOR_FAULT_ALREADY_STARTED = 60
_SUPERVISOR_FAULT_NOT_RUNNING = 70


class FakeSupervisorServer:
    """A supervisord-shaped XML-RPC server over a unix socket.

    Implements exactly the slice of the supervisor RPC namespace the liveness
    module uses -- ``getProcessInfo`` / ``startProcess`` / ``stopProcess`` --
    over ``statename_by_program``, with the same fault codes supervisord
    answers, so both the probe and the stop/start actions are tested against
    the real transport rather than a faked-out client.
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.statename_by_program: dict[str, str] = {}
        self._server = _UnixSocketXmlRpcServer(str(socket_path))
        self._server.register_function(self._get_process_info, "supervisor.getProcessInfo")
        self._server.register_function(self._start_process, "supervisor.startProcess")
        self._server.register_function(self._stop_process, "supervisor.stopProcess")
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # The dispatch protocol hands every RPC argument over as a marshallable
    # value, so the handlers take ``object`` and stringify -- exactly what the
    # wire delivers.
    def _get_process_info(self, name: object) -> dict[str, str]:
        program = str(name)
        if program not in self.statename_by_program:
            raise xmlrpc.client.Fault(_SUPERVISOR_FAULT_BAD_NAME, f"BAD_NAME: {program}")
        return {"name": program, "statename": self.statename_by_program[program]}

    def _start_process(self, name: object, _wait: object) -> bool:
        program = str(name)
        if program not in self.statename_by_program:
            raise xmlrpc.client.Fault(_SUPERVISOR_FAULT_BAD_NAME, f"BAD_NAME: {program}")
        if self.statename_by_program[program] in ("RUNNING", "STARTING"):
            raise xmlrpc.client.Fault(_SUPERVISOR_FAULT_ALREADY_STARTED, f"ALREADY_STARTED: {program}")
        self.statename_by_program[program] = "RUNNING"
        return True

    def _stop_process(self, name: object, _wait: object) -> bool:
        program = str(name)
        if program not in self.statename_by_program:
            raise xmlrpc.client.Fault(_SUPERVISOR_FAULT_BAD_NAME, f"BAD_NAME: {program}")
        if self.statename_by_program[program] not in ("RUNNING", "STARTING"):
            raise xmlrpc.client.Fault(_SUPERVISOR_FAULT_NOT_RUNNING, f"NOT_RUNNING: {program}")
        self.statename_by_program[program] = "STOPPED"
        return True


@contextmanager
def agent_message_lock(agent_state_dir: Path) -> Generator[None, None, None]:
    """Hold mngr's per-agent ``message.lock`` for the duration of the block (blocking acquire).

    A test-only helper: the conservation storms stage a completed in-flight send by taking the
    same exclusive flock mngr's send holds (``BaseAgent._message_lock`` -- same filename, same
    agent state dir) so a stop/flush executor under test contends with it exactly as it would in
    production. Nothing in production takes this blocking lock (the executors use the bounded
    ``try_hold_message_lock``), which is why it lives here rather than in the harness code.
    """
    lock_path = agent_state_dir / MESSAGE_LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def is_e2e_browser_installed() -> bool:
    """True when a Chromium the e2e suite can launch is present on this host.

    Either the workspace-provisioned Fortress build (which the
    ``browser_type_launch_args`` fixture prefers) or a browser in Playwright's
    own download cache satisfies the check; with neither present the e2e tests
    skip instead of erroring at browser launch.
    """
    if FORTRESS_CHROMIUM_PATH.exists():
        return True
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env_path:
        cache_dir = Path(env_path)
    elif sys.platform == "darwin":
        cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        cache_dir = Path.home() / ".cache" / "ms-playwright"
    return cache_dir.exists() and any(cache_dir.iterdir())


class RecordingMngrMessenger(MngrMessenger):
    """A `MngrMessenger` that records sends and key-chord presses and never contacts mngr.

    Overrides `send_to_agent` (records each `(agent_id, message)`) and
    `press_key_chord_to_agent` (records each `(agent_id, key)`), returning fixed
    results, so a test exercises the manager's send / keypress paths without building a
    real mngr context or hitting the network. Inject via
    `AgentManager.build(broadcaster, messenger=RecordingMngrMessenger())`.
    """

    sent: list[tuple[str, str]] = []
    pressed: list[tuple[str, str]] = []
    succeeds: bool = True
    # What a non-succeeding send reports, in place of a harness's own words and mngr's kind.
    failure_reason: str = "The agent could not be reached."
    failure_kind: str = "unknown"
    press_succeeds: bool = True

    def send_to_agent(
        self, agent_id: AgentId, message: str, known_locations: Sequence[AgentMatch]
    ) -> SendFailure | None:
        self.sent.append((str(agent_id), message))
        return None if self.succeeds else SendFailure(reason=self.failure_reason, kind=self.failure_kind)

    def press_key_chord_to_agent(self, agent_id: AgentId, key: str, known_locations: Sequence[AgentMatch]) -> bool:
        self.pressed.append((str(agent_id), key))
        return self.press_succeeds


def build_test_state(
    *,
    config: Config | None = None,
    agent_manager: AgentManager | None = None,
    claude_auth_service: ClaudeAuthService | None = None,
    auth_flows: AuthFlowService | None = None,
    welcome_resender: WelcomeResender | None = None,
    latchkey_http_client: httpx.Client | None = None,
) -> SystemInterfaceState:
    """Build a `SystemInterfaceState` for tests, injecting fakes where provided.

    Every collaborator left unset gets a cheap default production instance;
    pass one to substitute a fake. The agent manager is built but never started,
    so no `mngr observe` pipeline is spawned. The state's broadcaster is derived
    from the agent manager, so injecting `agent_manager` (often built with a fake
    `MngrMessenger`) repoints the broadcaster too.

    Only the collaborators tests actually override are parameters; the agent
    filters and the local-service http client (which no test substitutes) are
    fixed to their production defaults inline.
    """
    manager = agent_manager if agent_manager is not None else AgentManager.build(WebSocketBroadcaster())
    event_queues = AgentEventQueues()
    # Match production: route the codex ledger's live user-turns (Fix 1) onto the event fan-out.
    manager.set_transcript_broadcaster(event_queues.broadcast_all_ignored)
    return SystemInterfaceState(
        auth_flows=auth_flows if auth_flows is not None else AuthFlowService.create(),
        config=config if config is not None else Config(),
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
        agent_manager=manager,
        event_queues=event_queues,
        layout_mutex=LayoutMutex(),
        claude_auth_service=claude_auth_service if claude_auth_service is not None else ClaudeAuthService(),
        welcome_resender=welcome_resender
        if welcome_resender is not None
        else WelcomeResender(
            resolve_agent=manager.get_agent_info_by_id,
            send_message_fn=manager.send_message_to_agent,
        ),
        http_client=httpx.Client(follow_redirects=False, timeout=30.0),
        latchkey_http_client=latchkey_http_client if latchkey_http_client is not None else httpx.Client(timeout=30.0),
    )


class FakeFinishedProcess:
    """Minimal stand-in for a `FinishedProcess` returned by `command_runner`.

    The real subprocess runner produces an object with `stdout`, `stderr`,
    and `returncode`; this class exposes just those three so tests can
    drive every branch the `claude_auth` callers care about.
    """

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakePexpectProcess:
    """Scripted stand-in for a `pexpect.spawn` in the PTY auth flows.

    `expect_script` is a sequence of `(return_index, output_chunk)` pairs:
    each `expect()` call consumes the next entry (the final entry repeats
    once the script is exhausted), returns `return_index`, and exposes
    `output_chunk` through `before`/`after` the way pexpect does after a
    match (index 0: chunk in `after`) or a non-match (chunk in `before`).
    The return indexes are positions in the pattern list the production
    code passes to `expect`, so a test scripting e.g. the token pump must
    use that pump's pattern order.

    `read_nonblocking` (used by the production drain loop after a trigger
    match) yields `drain_chunks` one call at a time and then raises
    `pexpect.EOF`, so drains terminate immediately instead of spinning
    against their wall-clock deadline.
    """

    def __init__(
        self,
        expect_script: Sequence[tuple[int, str]],
        drain_chunks: Sequence[str] = (),
        is_alive: bool = True,
    ) -> None:
        assert expect_script, "expect_script must have at least one entry"
        self._script = list(expect_script)
        # Hardcoding this True made every "the CLI has exited" arm unreachable from tests --
        # including the only success signal codex's device flow has, which is process exit.
        self._is_alive = is_alive
        self._call_idx = 0
        self._drain_chunks = list(drain_chunks)
        self.sendline_calls: list[str] = []
        self.send_calls: list[str] = []
        self.terminate_calls = 0
        self.close_calls = 0
        self.timeout: float | None = None
        self.before = ""
        self.after: str = ""

    def expect(self, _patterns: object, timeout: float | None = None) -> int:
        entry_idx = min(self._call_idx, len(self._script) - 1)
        self._call_idx += 1
        return_index, chunk = self._script[entry_idx]
        if return_index == 0:
            self.before = ""
            self.after = chunk
        else:
            self.before = chunk
            self.after = ""
        return return_index

    def read_nonblocking(self, size: int = 65536, timeout: float | None = None) -> str:
        if self._drain_chunks:
            return self._drain_chunks.pop(0)
        raise pexpect.EOF("fake stream exhausted")

    def sendline(self, s: str) -> None:
        self.sendline_calls.append(s)

    def send(self, s: str) -> None:
        self.send_calls.append(s)

    def isalive(self) -> bool:
        return self._is_alive

    def exit(self) -> None:
        """Let the scripted CLI finish. `terminate` does not: the production teardown calls
        it on paths where the process was already gone, so it cannot mean "now exited"."""
        self._is_alive = False

    def terminate(self, force: bool = False) -> None:
        self.terminate_calls += 1

    def close(self) -> None:
        self.close_calls += 1



def _find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until_serving(host: str, port: int, timeout: float = 10.0) -> None:
    """Poll a TCP connect until the server accepts, or raise on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with closing(socket.create_connection((host, port), timeout=0.5)):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"server at {host}:{port} did not start within {timeout}s")


class ServedApp:
    """Handle to a Flask app served by a real Werkzeug listener in a background thread."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


@contextmanager
def serve_app(app: Flask) -> Iterator[ServedApp]:
    """Serve ``app`` on an ephemeral loopback port via a real threaded Werkzeug server.

    Used by the WebSocket/SSE tests, which the Flask test client cannot drive
    (flask-sock needs a real listener). The server runs in a daemon thread and
    is shut down on exit.
    """
    host = "127.0.0.1"
    port = _find_free_port()
    server = make_threaded_server(host, port, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_until_serving(host, port)
        yield ServedApp(host, port)
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


def open_ws(served: ServedApp, path: str, subprotocols: list[str] | None = None) -> simple_websocket.Client:
    """Open a WebSocket client against a ``ServedApp`` at ``path``."""
    return simple_websocket.Client(f"{served.ws_url}{path}", subprotocols=subprotocols)


def close_ws(ws: simple_websocket.Client) -> None:
    """Close a WebSocket client, tolerating an already-closed connection.

    A handler that finishes (e.g. the proto-agent-logs not-found path) closes
    the socket server-side first, so the client-side close races the client's
    background thread processing that server close. Depending on how far that
    thread has gotten, ``ws.close()`` raises either ``ConnectionClosed`` (the
    close was fully processed and ``connected`` is already False) or ``OSError``
    (EBADF: the thread tore down the socket fd between ``close()``'s
    ``connected`` check and its send of the close frame).
    """
    try:
        ws.close()
    except (simple_websocket.ConnectionClosed, OSError):
        pass
