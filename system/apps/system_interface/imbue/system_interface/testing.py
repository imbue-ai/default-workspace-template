"""Shared test fakes for the system_interface package.

Houses the supervisord-shaped fake the liveness tests and the stop/start routes run
against, the loopback server the WebSocket tests need, and `build_test_state`, the
test-side composition root: it builds a `SystemInterfaceState` over a fresh state
directory, mirroring `main.build_production_state` without ever starting the shell.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import xmlrpc.client
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final
from xmlrpc.server import SimpleXMLRPCDispatcher
from xmlrpc.server import SimpleXMLRPCRequestHandler

import simple_websocket
from app_manifest.registry import registry_path
from flask import Flask
from pydantic import Field

from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.config import Config
from imbue.system_interface.shell.inventory import AppInventory
from imbue.system_interface.shell.state import build_shell_state
from imbue.system_interface.template_catalog import TemplateCatalogFetcherInterface
from imbue.system_interface.template_catalog import TemplateCatalogStore
from imbue.system_interface.template_catalog import build_template_catalog_store
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
    module uses -- ``getAllProcessInfo`` / ``startProcess`` / ``stopProcess``
    -- over ``statename_by_program``, with the same fault codes supervisord
    answers, so both the probes and the stop/start actions are tested against
    the real transport rather than a faked-out client.
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.statename_by_program: dict[str, str] = {}
        # Lets tests assert on the sweep's RPC economy (e.g. that a registry
        # with no supervised rows makes no supervisord call at all).
        self.get_all_process_info_call_count = 0
        self._server = _UnixSocketXmlRpcServer(str(socket_path))
        self._server.register_function(self._get_all_process_info, "supervisor.getAllProcessInfo")
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
    def _get_all_process_info(self) -> list[dict[str, str]]:
        self.get_all_process_info_call_count += 1
        return [{"name": program, "statename": statename} for program, statename in self.statename_by_program.items()]

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


# The directories minted for states built without a ``shell_state_directory``; each is removed
# by its finalizer when the test process exits, so a run leaves nothing under the temp root.
_MINTED_SHELL_STATE_DIRECTORIES: Final[list[tempfile.TemporaryDirectory[str]]] = []


def _fresh_shell_state_directory() -> Path:
    directory = tempfile.TemporaryDirectory(prefix="si-shell-state-")
    _MINTED_SHELL_STATE_DIRECTORIES.append(directory)
    return Path(directory.name)


class FakeTemplateCatalogFetcher(TemplateCatalogFetcherInterface):
    """Answers each catalog URL from a table (None for one not in it) and records every fetch."""

    body_by_url: dict[str, bytes] = Field(default_factory=dict, description="What each URL answers")
    fetched_urls: list[str] = Field(default_factory=list, description="Every URL fetched, in order")

    def fetch(self, url: str) -> bytes | None:
        self.fetched_urls.append(url)
        return self.body_by_url.get(url)


def catalog_template_document(slug: str, **overrides: Any) -> dict[str, Any]:
    """One template as a catalog lists it: the four required fields and a relative drawing, derived
    from the slug, with ``overrides`` laid over them."""
    document: dict[str, Any] = {
        "slug": slug,
        "title": slug.title(),
        "description": f"What {slug} does.",
        "repository_url": f"https://github.com/someone/{slug}",
        "thumbnail": f"thumbnails/someone--{slug}.svg",
    }
    document.update(overrides)
    return document


def catalog_document(
    *templates: Mapping[str, Any], shelves: Sequence[Mapping[str, Any]] = (), **overrides: Any
) -> bytes:
    """A format-1 catalog document as a fetcher answers it, holding ``templates`` and ``shelves``."""
    document: dict[str, Any] = {
        "format": 1,
        "generated_at": "2026-09-07T00:00:00Z",
        "templates": list(templates),
        "shelves": list(shelves),
    }
    document.update(overrides)
    return json.dumps(document).encode("utf-8")


def build_test_state(
    *,
    config: Config | None = None,
    broadcaster: WebSocketBroadcaster | None = None,
    shell_state_directory: Path | None = None,
    inventory: AppInventory | None = None,
    template_catalog_fetcher: TemplateCatalogFetcherInterface | None = None,
) -> SystemInterfaceState:
    """Build a `SystemInterfaceState` for tests, injecting fakes where provided.

    The shell state is built but never started, so no registry watch or inventory sweep
    runs. ``shell_state_directory`` is where the shell's state files go (a fresh temp
    directory by default); ``inventory`` substitutes an inventory built over a fake fetcher,
    and ``broadcaster`` the fan-out the inventory and the routes share. The template catalog
    is disabled (no URL) unless a ``template_catalog_fetcher`` is given, so no test reaches
    the network for it; with one, the store fetches the config's URL through it.
    """
    state_directory = shell_state_directory if shell_state_directory is not None else _fresh_shell_state_directory()
    resolved_config = config if config is not None else Config()
    shell = build_shell_state(
        state_directory=state_directory,
        registry_path=registry_path(),
        broadcaster=broadcaster if broadcaster is not None else WebSocketBroadcaster(),
        inventory=inventory,
    )
    template_catalog = build_template_catalog_store(
        catalog_url=resolved_config.system_interface_template_catalog_url
        if template_catalog_fetcher is not None
        else "",
        state_directory=state_directory,
        fetcher=template_catalog_fetcher,
    )
    return SystemInterfaceState(config=resolved_config, shell=shell, template_catalog=template_catalog)


def build_disabled_template_catalog_store(state_directory: Path) -> TemplateCatalogStore:
    """A store with no URL: what a test state gets unless it injects a fetcher."""
    return build_template_catalog_store(catalog_url="", state_directory=state_directory)


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
