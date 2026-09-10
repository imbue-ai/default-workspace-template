"""Shared test fakes for the chat package.

Houses deterministic stand-ins for outside-world dependencies that
`ClaudeAuthService` takes as constructor-injected callables
(`command_runner`, `pexpect_spawner`). Both `claude_auth_test.py` and
`harnesses/claude/auth_endpoints_test.py` need the same fakes, so they live here
rather than being copy-pasted into each test module.

Also houses `build_test_state`, the test-side composition root: it builds a
`ChatState` with fakes for whichever collaborators a test overrides and cheap real
instances for the rest, mirroring `main.build_production_state` without ever starting
the agent manager.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import closing
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from typing import Final
from unittest.mock import patch

import httpx
import pexpect
import simple_websocket
from app_instances.blueprint import build_instances_app
from app_instances.nudge import ShellNudger
from app_instances.sidecar import serve_in_background
from app_instances.testing import LOOPBACK_HOST
from app_instances.testing import StubInstanceSource
from app_instances.testing import free_port
from app_manifest.primitives import AppName
from flask import Flask
from flask import request
from pydantic import Field

from imbue.chat.accounts import commit_account
from imbue.chat.accounts import mint_account_dir
from imbue.chat.activity_state import ActivityState
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.agent_discovery import MngrMessenger
from imbue.chat.agent_discovery import SendFailure
from imbue.chat.agent_manager import AgentManager
from imbue.chat.config import Config
from imbue.chat.create_defaults import TYPE_KEY
from imbue.chat.event_queues import AgentEventQueues
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.claude.auth import ClaudeAuthService
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.interrupt import MESSAGE_LOCK_FILENAME
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.models import AgentStateItem
from imbue.chat.server import create_application
from imbue.chat.state import ChatState
from imbue.chat.ws_broadcaster import WebSocketBroadcaster
from imbue.chat.wsgi import make_threaded_server
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config as ShellConfig
from imbue.system_interface.server import create_application as create_shell_application
from imbue.system_interface.shell.testing import instance_record
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import build_test_state as build_shell_test_state
from imbue.system_interface.wsgi import make_threaded_server as make_shell_server

# The workspace's browser engine is Fortress (a stealth-patched Chromium fork)
# provisioned by env-converge. Playwright's own browser-cache lookup only
# auto-discovers builds Playwright downloaded itself, so launches must name
# this binary explicitly via ``executable_path`` (see the
# ``browser_type_launch_args`` fixture override in ``conftest.py``).
FORTRESS_CHROMIUM_PATH = Path("/opt/fortress/tilion-fortress/tilion")


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


def seed_agent_state(
    manager: AgentManager,
    agent_id: str,
    *,
    name: str,
    state: str = "RUNNING",
    labels: Mapping[str, str] | None = None,
    harness: HarnessType = HarnessType.CLAUDE,
    activity_state: ActivityState | None = None,
) -> None:
    """Insert an ``AgentStateItem`` straight into the manager's tracked map, bypassing discovery."""
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name=name,
            state=state,
            labels=dict(labels) if labels is not None else {},
            work_dir=None,
            harness=harness,
            activity_state=activity_state,
        )


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


class RecordingShell(MutableModel):
    """A shell for the auto-open reactor whose connected clients a test sets, recording every open it is asked for."""

    model_config = {"extra": "forbid", "frozen": False}

    client_ids: list[str] = []
    refused_client_ids: list[str] = []
    opens: list[tuple[str, str]] = []

    def connected_client_ids(self) -> list[str]:
        return list(self.client_ids)

    def open_chat(self, agent_id: str, client_id: str) -> bool:
        self.opens.append((agent_id, client_id))
        return client_id not in self.refused_client_ids


def read_create_defaults_type(path: Path) -> str | None:
    """The `commands.create.type` the workspace's local mngr settings name, or None when they name none."""
    if not path.exists():
        return None
    raw = tomllib.loads(path.read_text())
    create = raw.get("commands", {}).get("create", {})
    agent_type = create.get(TYPE_KEY) if isinstance(create, dict) else None
    return agent_type if isinstance(agent_type, str) and agent_type else None


class RecordingClientActivityShell:
    """A stand-in shell that records every body posted to ``/api/client-activity``."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.application = Flask("recording-shell")
        self.application.add_url_rule(
            "/api/client-activity", view_func=self._accept, methods=["POST"], endpoint="client_activity"
        )

    def _accept(self) -> tuple[str, int]:
        self.received.append(request.get_json())
        return "", 204


def build_test_state(
    *,
    config: Config | None = None,
    agent_manager: AgentManager | None = None,
    claude_auth_service: ClaudeAuthService | None = None,
    auth_flows: AuthFlowService | None = None,
    latchkey_http_client: httpx.Client | None = None,
) -> ChatState:
    """Build a `ChatState` for tests, injecting fakes where provided.

    Every collaborator left unset gets a cheap default production instance;
    pass one to substitute a fake. The agent manager is built but never started,
    so no `mngr observe` pipeline is spawned. The state's broadcaster is derived
    from the agent manager, so injecting `agent_manager` (often built with a fake
    `MngrMessenger`) repoints the broadcaster too.
    """
    manager = agent_manager if agent_manager is not None else AgentManager.build(WebSocketBroadcaster())
    event_queues = AgentEventQueues()
    # Match production: route the codex ledger's live user-turns onto the event fan-out.
    manager.set_transcript_broadcaster(event_queues.broadcast_batch)
    state = ChatState(
        # Never the production probe: it shells out to whatever claude/codex/agy/pi this
        # machine happens to have, over the network, from any test that reaches a sign-in
        # route. UNKNOWN is the honest stand-in -- "the check could not run" -- and a test
        # that cares about the verdict injects its own service.
        auth_flows=auth_flows
        if auth_flows is not None
        else AuthFlowService.create(probe=lambda *_args: SignedIn.UNKNOWN),
        config=config if config is not None else Config(),
        provider_names=None,
        include_filters=(),
        exclude_filters=(),
        agent_manager=manager,
        event_queues=event_queues,
        claude_auth_service=claude_auth_service if claude_auth_service is not None else ClaudeAuthService(),
        http_client=httpx.Client(follow_redirects=False, timeout=30.0),
        latchkey_http_client=latchkey_http_client if latchkey_http_client is not None else httpx.Client(timeout=30.0),
    )
    # Match production: eviction drops a destroyed/stopped agent's watcher.
    manager.set_watcher_eviction_callback(state.stop_and_remove_watcher)
    return state


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
        # Scriptable so the "the CLI has exited" arms are reachable from tests: process exit
        # is the only success signal codex's device flow has.
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
    port = free_port()
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

    A handler that finishes first (the ``/api/ws`` loop exiting on the broadcaster's
    shutdown sentinel) closes the socket server-side, so the client-side close races the client's
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


# ---------- the two-process fixture: the shell framing this chat app ----------

# The fixture chat's agent id and name, and the project every workspace starts with unless a
# test asks for none (what a migrated workspace has, and where a fresh browser lands).
FIXTURE_AGENT_ID: Final[str] = "agent-test-123"
FIXTURE_AGENT_NAME: Final[str] = "test-agent"
FIXTURE_CHAT_ADDRESS: Final[str] = f"app:chat?instance={FIXTURE_AGENT_ID}"
STARTER_PROJECT_NAME: Final[str] = "Project 1"
STARTER_PROJECT_ID: Final[str] = "project-1"
FIXTURE_SESSION_ID: Final[str] = "e2e-session-001"

# The stub app a workspace offers beside the chat when a test asks for one.
STUB_APP_NAME: Final[str] = "docs"
STUB_APP_DISPLAY_NAME: Final[str] = "Docs"
STUB_NEW_ACTION_LABEL: Final[str] = "New docs"

_FIXTURE_SESSION_EVENTS: Final[list[dict[str, Any]]] = [
    {
        "type": "user",
        "uuid": "uuid-1",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"role": "user", "content": "Hello agent!"},
    },
    {
        "type": "assistant",
        "uuid": "uuid-2",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [{"type": "text", "text": "Hello! How can I help you?"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    },
]


def make_session_file(projects_dir: Path, session_id: str, events: Sequence[Mapping[str, Any]]) -> Path:
    """Write a claude session JSONL under a fixture config dir and return its path."""
    projects_dir.mkdir(parents=True, exist_ok=True)
    session_file = projects_dir / f"{session_id}.jsonl"
    with open(session_file, "w") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    return session_file


def make_agent_fixture(
    tmp_path: Path,
    agent_id: str = FIXTURE_AGENT_ID,
    agent_name: str = FIXTURE_AGENT_NAME,
    session_events: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[AgentInfo, Path]:
    """A fake agent state dir plus a claude config dir holding one session file; returns the agent and the file."""
    agent_state_dir = tmp_path / "agents" / agent_id
    agent_state_dir.mkdir(parents=True, exist_ok=True)
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "test-project"
    # The watcher finds the transcript by the session id the agent's state dir records, under the
    # CLAUDE_CONFIG_DIR its env file names; without both it falls back to the real ~/.claude and
    # the fixture transcript never loads.
    (agent_state_dir / "claude_session_id_history").write_text(f"{FIXTURE_SESSION_ID}\n")
    (agent_state_dir / "env").write_text(f"CLAUDE_CONFIG_DIR={claude_config_dir}\n")
    session_file = make_session_file(
        projects_dir, FIXTURE_SESSION_ID, session_events if session_events is not None else _FIXTURE_SESSION_EVENTS
    )
    agent_info = AgentInfo(
        id=agent_id,
        name=agent_name,
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        work_dir=str(tmp_path / "work"),
    )
    return agent_info, session_file


class RunningWorkspace(FrozenModel):
    """Handle to a shell and a chat app served together, with the fixtures behind the chat."""

    model_config = {"arbitrary_types_allowed": True}

    shell_url: str = Field(description="The shell's loopback URL")
    chat_url: str = Field(description="The chat app's loopback URL, where its pages are framed from")
    agent_info: AgentInfo = Field(description="The fixture chat's agent")
    session_file: Path = Field(description="The fixture chat's session file, appended to for streaming tests")
    state_dir: Path = Field(description="The shell's state directory")
    chat_state: ChatState = Field(description="The chat app's state, for the manager behind its routes")
    stub_source: StubInstanceSource | None = Field(description="The stub app's instances, when offered")
    stub_url: str | None = Field(description="The stub app's loopback URL, when offered")


def _is_serving_api(base_url: str) -> bool:
    try:
        urllib.request.urlopen(f"{base_url}/api/health", timeout=0.5)
        return True
    except urllib.error.HTTPError:
        return True
    except OSError:
        return False


def _post_json(url: str, body: Mapping[str, Any]) -> None:
    request_body = json.dumps(dict(body)).encode()
    posted = urllib.request.Request(
        url, data=request_body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(posted, timeout=5):
        pass


def _write_fake_binaries(tmp_path: Path) -> Path:
    """A fake logged-in ``claude`` and a fake ``mngr`` that accepts everything, for the paths that shell out."""
    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir(exist_ok=True)
    fake_claude = fake_bin_dir / "claude"
    fake_claude.write_text(
        '#!/bin/sh\necho \'{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}\'\n'
    )
    fake_claude.chmod(0o755)
    fake_mngr = fake_bin_dir / "mngr"
    # A create takes a beat, so a page opened on a chat being created is seen in that phase;
    # ``FAKE_MNGR_CREATE_EXIT_CODE`` in the environment makes it fail with that status.
    fake_mngr.write_text(
        '#!/bin/sh\ncase "$1" in create) sleep 2; echo "create failed on purpose" >&2; '
        'exit "${FAKE_MNGR_CREATE_EXIT_CODE:-0}" ;; esac\nexit 0\n'
    )
    fake_mngr.chmod(0o755)
    return fake_bin_dir


@contextmanager
def running_workspace(
    tmp_path: Path,
    shell_port: int,
    chat_port: int,
    session_events: Sequence[Mapping[str, Any]] | None = None,
    additional_agents: Sequence[tuple[str, str]] = (),
    is_stub_app_offered: bool = False,
    stub_instances: Sequence[str] = (),
    project_names: Sequence[str] = (STARTER_PROJECT_NAME,),
    is_account_signed_in: bool = True,
) -> Iterator[RunningWorkspace]:
    """Serve the shell and this chat app together, the way a workspace runs them, over fakes.

    The chat app lists the fixture agent (plus any ``additional_agents``, bare state dirs with
    a manager entry) from a patched discovery and a never-started manager, so no ``mngr observe``
    runs; the shell reads a registry holding the chat row at the chat's own URL and, when
    ``is_stub_app_offered``, a stub app whose ``stub_instances`` are seeded as records. With
    ``is_account_signed_in`` (the default) a signed-in account exists, so a create starts at
    once; without one a create mints a chat that waits for an account, and its page offers
    the provider chooser. ``project_names``
    are created through the shell's API before anything connects, so a client's first view is
    the first of them (or Everything when there are none).
    """
    shell_url = f"http://127.0.0.1:{shell_port}"
    chat_url = f"http://127.0.0.1:{chat_port}"
    # The work dir every agent reports, and the one a create runs in.
    (tmp_path / "work").mkdir(exist_ok=True)
    agent_info, session_file = make_agent_fixture(tmp_path, session_events=session_events)
    extra_infos: list[AgentInfo] = []
    for extra_id, extra_name in additional_agents:
        extra_state_dir = tmp_path / "agents" / extra_id
        extra_state_dir.mkdir(parents=True, exist_ok=True)
        extra_infos.append(
            AgentInfo(
                id=extra_id,
                name=extra_name,
                state="RUNNING",
                agent_state_dir=extra_state_dir,
                claude_config_dir=agent_info.claude_config_dir,
            )
        )
    agents = [agent_info, *extra_infos]
    fake_bin_dir = _write_fake_binaries(tmp_path)

    registry_path = tmp_path / "registry" / "apps.toml"
    rows = [
        registry_row_toml(
            "chat",
            chat_url,
            is_multi_instance=True,
            is_critical=True,
            actions=(("new", "New Chat"), ("subagent", "Open subagent")),
            default_shortcut=("new", "new"),
            display_name="Chat",
        )
    ]
    stub_source: StubInstanceSource | None = None
    stub_url: str | None = None
    stub_port = free_port()
    if is_stub_app_offered:
        stub_source = StubInstanceSource()
        for key in stub_instances:
            stub_source.records.append(instance_record(key, title=f"Stub {key.removeprefix('stub-')}"))
        stub_url = f"http://{LOOPBACK_HOST}:{stub_port}"
        rows.append(
            registry_row_toml(
                STUB_APP_NAME,
                stub_url,
                is_multi_instance=True,
                actions=(("new", STUB_NEW_ACTION_LABEL),),
                default_shortcut=("new", "focus"),
                display_name=STUB_APP_DISPLAY_NAME,
            )
        )
    write_registry(registry_path, *rows)

    with (
        patch.dict(
            os.environ,
            {
                "MNGR_HOST_DIR": str(tmp_path),
                "MNGR_AGENT_ID": "",
                "MNGR_AGENT_WORK_DIR": str(tmp_path / "work"),
                "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
                "MINDS_ACCOUNTS_ROOT": str(tmp_path / "accounts"),
                "MINDS_APPS_FILE": str(registry_path),
                "MINDS_WORKSPACE_SERVER_URL": shell_url,
            },
        ),
        patch("imbue.chat.server.discover_agents", return_value=agents),
    ):
        # A signed-in account is what a new chat launches on at once; without one the chat's
        # ``new`` mints a chat that waits for an account (its page shows the provider chooser).
        if is_account_signed_in:
            account_id, _ = mint_account_dir()
            commit_account(account_id, "anthropic", "Anthropic")

        manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
        with manager._lock:
            for info in agents:
                manager._agents[info.id] = AgentStateItem(
                    id=info.id, name=info.name, state="RUNNING", labels={}, work_dir=str(tmp_path / "work")
                )
        for info in agents:
            manager._ensure_activity_tracking(info.id)
        manager.note_agent_list_known()
        # The chat nudges the shell the way the real process does, so a change lists at once.
        manager.set_nudger(ShellNudger(app_name=AppName("chat"), shell_url=shell_url))
        chat_state = build_test_state(config=Config(chat_host="127.0.0.1", chat_port=chat_port), agent_manager=manager)
        chat_app = create_application(chat_state)
        chat_server = make_threaded_server("127.0.0.1", chat_port, chat_app)
        chat_thread = threading.Thread(target=chat_server.serve_forever, daemon=True)
        chat_thread.start()

        state_dir = tmp_path / "shell-state"
        shell_state = build_shell_test_state(
            config=ShellConfig(system_interface_host="127.0.0.1", system_interface_port=shell_port),
            shell_state_directory=state_dir,
        )
        shell_app = create_shell_application(shell_state)
        shell_server = make_shell_server("127.0.0.1", shell_port, shell_app)
        shell_thread = threading.Thread(target=shell_server.serve_forever, daemon=True)
        shell_thread.start()
        stub_server = (
            serve_in_background(
                LOOPBACK_HOST,
                stub_port,
                build_instances_app(stub_source, ShellNudger(app_name=AppName(STUB_APP_NAME), shell_url=shell_url)),
            )
            if stub_source is not None
            else nullcontext()
        )
        with stub_server:
            try:
                for url in (shell_url, chat_url):
                    wait_for(
                        lambda url=url: _is_serving_api(url),
                        timeout=10.0,
                        poll_interval=0.1,
                        error_message=f"the server at {url} did not come up",
                    )
                for name in project_names:
                    _post_json(f"{shell_url}/api/projects", {"name": name, "color": "#3B82F6", "glyph": 1})
                # Started only once the apps are serving: the first instance fetch must find
                # the chat app and the stub app answering.
                shell_state.shell.start()
                try:
                    yield RunningWorkspace(
                        shell_url=shell_url,
                        chat_url=chat_url,
                        agent_info=agent_info,
                        session_file=session_file,
                        state_dir=state_dir,
                        chat_state=chat_state,
                        stub_source=stub_source,
                        stub_url=stub_url,
                    )
                finally:
                    shell_state.shell.stop()
            finally:
                shell_server.shutdown()
                shell_thread.join(timeout=5.0)
                chat_server.shutdown()
                chat_thread.join(timeout=5.0)
                chat_state.shutdown()
