import json
import os
import queue
import socket
import subprocess
import tempfile
import threading
from collections.abc import Generator
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger as loguru_logger
from playwright.sync_api import Browser
from playwright.sync_api import BrowserType
from playwright.sync_api import Playwright
from playwright.sync_api import sync_playwright

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.config import Config
from imbue.system_interface.server import create_application
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import write_registry
from imbue.system_interface.testing import FORTRESS_CHROMIUM_PATH
from imbue.system_interface.testing import FakeSupervisorServer
from imbue.system_interface.testing import PIPELINE_BASE_URL
from imbue.system_interface.testing import PIPELINE_CLIENT_ID
from imbue.system_interface.testing import PIPELINE_DEFAULT_DESKTOP_ID
from imbue.system_interface.testing import PIPELINE_PORT
from imbue.system_interface.testing import PIPELINE_SEEDED_APP_NAME
from imbue.system_interface.testing import PIPELINE_STUB_APP_NAME
from imbue.system_interface.testing import PipelineHarness
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import is_server_answering
from imbue.system_interface.testing import serve_app
from imbue.system_interface.testing import stand_in_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster
from imbue.system_interface.wsgi import make_threaded_server


@pytest.fixture(autouse=True)
def _isolate_system_interface_tests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Keep every shell test away from the live workspace's registry and shell.

    The shell's inventory reads the app registry from the working directory otherwise, which
    in a workspace is the live one; and the scripts a test drives (``layout.py``,
    ``refresh_workspace_view.py``) post to the shell ``MINDS_WORKSPACE_SERVER_URL`` names, which
    would be the workspace's real one. A port nothing listens on refuses them at once; the
    pipeline and e2e tests serve a shell of their own and point at it.
    """
    monkeypatch.setenv("MINDS_APPS_FILE", str(tmp_path_factory.mktemp("minds-registry") / "apps.toml"))
    monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", "http://127.0.0.1:1")


# --- pytest-playwright fixture-scope overrides -------------------------------
#
# pytest-playwright (installed as a plugin) ships these fixtures at SESSION
# scope: `playwright` (the sync_playwright handle, which spawns the node
# driver subprocess), `browser_type`, `browser_type_launch_args`,
# `connect_options`, and `browser` (the actual chromium/firefox process).
# Session-scope means teardown runs at pytest session end -- AFTER mngr's
# autouse `session_cleanup` fixture (libs/mngr/imbue/mngr/conftest.py) has
# already checked for leaked child processes. In offload release batches
# that mix system_interface e2e tests with other mngr tests, both the
# playwright node driver and chrome-headless-shell are still alive when
# session_cleanup runs, so it asserts "leftover child processes" and
# cascades a teardown error into every sibling test in the batch
# (test_install.py, test_help.py, test_release_vultr, etc.).
#
# The fix is to force the entire fixture chain down to function scope so
# each test's playwright+chrome teardown finishes inside its own pytest
# teardown. Cost: a second or so per test to re-spawn the driver+browser;
# trivial for the tiny e2e suite here.
#
# All four session-scoped fixtures must be overridden together because
# pytest forbids a session-scope fixture from depending on a function-scope
# one ("ScopeMismatch"). Overriding `browser` alone would leave
# `browser_type` at session scope and trip that check.


@pytest.fixture
def playwright() -> Generator[Playwright, None, None]:
    pw = sync_playwright().start()
    try:
        yield pw
    finally:
        # try/finally guards the node-driver subprocess against teardown-path
        # errors. The whole point of overriding this fixture to function scope
        # is to keep the driver out of session_cleanup's leaked-child check;
        # a mid-teardown error reaching the yield line without try/finally
        # would re-introduce that leak.
        pw.stop()


@pytest.fixture
def browser_type(playwright: Playwright) -> BrowserType:
    return playwright.chromium


@pytest.fixture
def browser_type_launch_args(pytestconfig: pytest.Config) -> dict[str, Any]:
    # Mirrors pytest-playwright's upstream browser_type_launch_args body
    # (see .venv/.../pytest_playwright/pytest_playwright.py `browser_type_launch_args`).
    # Do not add `device` here -- that's a context-level option consumed by
    # browser_context_args in upstream, not a valid kwarg for
    # browser_type.launch(), which would raise TypeError.
    launch_options: dict[str, Any] = {}
    headed = pytestconfig.getoption("--headed", default=False)
    if headed:
        launch_options["headless"] = False
    browser_channel = pytestconfig.getoption("--browser-channel", default=None)
    if browser_channel:
        launch_options["channel"] = browser_channel
    elif FORTRESS_CHROMIUM_PATH.exists():
        # Prefer the workspace-provisioned Fortress build over Playwright's
        # downloaded chromium/headless-shell, which is absent in a fresh
        # workspace (only env-converge installs a browser here). An explicit
        # ``--browser-channel`` wins because Playwright rejects a launch that
        # names both ``channel`` and ``executable_path``.
        launch_options["executable_path"] = str(FORTRESS_CHROMIUM_PATH)
    else:
        # No channel requested and no Fortress install (e.g. CI hosts that ran
        # `playwright install`): leave both keys unset so the launch falls
        # through to Playwright's managed-browser lookup.
        pass
    slowmo = pytestconfig.getoption("--slowmo", default=0)
    if slowmo:
        launch_options["slow_mo"] = slowmo
    return launch_options


@pytest.fixture
def connect_options() -> dict[str, Any] | None:
    return None


def _launch_playwright_browser(
    browser_type_launch_args: dict[str, Any],
    browser_type: BrowserType,
    connect_options: dict[str, Any] | None,
) -> Browser:
    """Launch or connect to a playwright browser using the fixture-provided args."""
    if connect_options:
        # Copied verbatim from pytest-playwright's upstream launch_browser
        # fixture. ty cannot verify the dynamic **connect_options spread
        # against connect's typed parameters (ws_endpoint: str, timeout,
        # headers, expose_network); the dict shape is dictated by
        # pytest-playwright's extension point for remote-browser use and
        # we mirror it exactly so downstream overrides stay compatible.
        return browser_type.connect(
            **{  # ty: ignore[invalid-argument-type]
                **connect_options,
                "headers": {
                    "x-playwright-launch-options": json.dumps(browser_type_launch_args),
                    **(connect_options.get("headers") or {}),
                },
            }
        )
    return browser_type.launch(**browser_type_launch_args)


@pytest.fixture
def browser_context_args(
    pytestconfig: pytest.Config,
    playwright: Playwright,
    device: str | None,
    base_url: str | None,
    _pw_artifacts_folder: tempfile.TemporaryDirectory,
) -> dict[str, Any]:
    # Mirrors pytest-playwright's upstream browser_context_args, overridden
    # to function scope because it transitively depends on `playwright`,
    # which we've pinned to function scope above. Without this override
    # pytest raises ScopeMismatch at setup time for every test that uses
    # `page` / `context` (i.e. the entire system_interface e2e suite).
    context_args: dict[str, Any] = {}
    if device:
        context_args.update(playwright.devices[device])
    if base_url:
        context_args["base_url"] = base_url
    video_option = pytestconfig.getoption("--video", default="off")
    if video_option in ("on", "retain-on-failure"):
        context_args["record_video_dir"] = _pw_artifacts_folder.name
    return context_args


@pytest.fixture
def browser(
    browser_type_launch_args: dict[str, Any],
    browser_type: BrowserType,
    connect_options: dict[str, Any] | None,
) -> Generator[Browser, None, None]:
    browser_instance = _launch_playwright_browser(
        browser_type_launch_args=browser_type_launch_args,
        browser_type=browser_type,
        connect_options=connect_options,
    )
    try:
        yield browser_instance
    finally:
        # try/finally guards chrome-headless-shell against teardown-path
        # errors. Matches the rationale on the `playwright` fixture above:
        # without it a mid-teardown error can leak the browser subprocess
        # into session_cleanup's leaked-child check.
        browser_instance.close()


@pytest.fixture
def broadcaster() -> WebSocketBroadcaster:
    return WebSocketBroadcaster()


@pytest.fixture
def listening_port() -> Iterator[int]:
    """A loopback port with a live listener behind it, for TCP liveness probes."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


@pytest.fixture
def closed_port() -> int:
    """A loopback port with nothing behind it.

    Bind-then-close: the port existed a moment ago, so nothing else is likely
    to have claimed it before the probe runs.
    """
    probe_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_socket.bind(("127.0.0.1", 0))
    port = probe_socket.getsockname()[1]
    probe_socket.close()
    return port


@pytest.fixture
def fake_supervisor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSupervisorServer]:
    """A supervisord-shaped RPC server on a per-test unix socket.

    ``MINDS_SUPERVISOR_SOCKET`` is pointed at it, so both the liveness probes
    and the stop/start endpoints reach the fake instead of the developer's (or
    CI's absent) real supervisord.
    """
    server = FakeSupervisorServer(tmp_path / "supervisor.sock")
    monkeypatch.setenv("MINDS_SUPERVISOR_SOCKET", str(server.socket_path))
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def loguru_records() -> Iterator[list[str]]:
    """Capture loguru log messages as plain strings for test assertions.

    Each entry in the yielded list is a ``"<LEVEL> <message>"`` line, so tests
    can filter on both level and text without wiring up loguru into pytest's
    stdlib-oriented ``caplog``.
    """
    messages: list[str] = []
    handler_id = loguru_logger.add(
        lambda msg: messages.append(f"{msg.record['level'].name} {msg.record['message']}"),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


@pytest.fixture
def git_work_dir(tmp_path: Path) -> Path:
    """Create a minimal git repository for tests that need a real git work directory."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        },
    )
    return tmp_path


@pytest.fixture
def layout_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[PipelineHarness, None, None]:
    """A workspace server over a registry of two stand-in apps, one declaring launch paths, and a started shell:
    what ``test_layout_pipeline.py`` drives ``layout.py`` against."""
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry_path))
    monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", PIPELINE_BASE_URL)

    with serve_app(stand_in_app()) as seeded, serve_app(stand_in_app()) as stub:
        write_registry(
            registry_path,
            registry_row_toml(
                PIPELINE_SEEDED_APP_NAME,
                seeded.http_url,
                is_critical=True,
                default_shortcut=("new", "new"),
                launch_paths=(("new", "New Chat", "/new"), ("subagent", "Open subagent", "/subagent")),
            ),
            registry_row_toml(PIPELINE_STUB_APP_NAME, stub.http_url),
        )

        broadcaster = WebSocketBroadcaster()
        config = Config(system_interface_host="127.0.0.1", system_interface_port=PIPELINE_PORT)
        state = build_test_state(config=config, broadcaster=broadcaster, shell_state_directory=tmp_path / "shell")
        app = create_application(state)

        server = make_threaded_server("127.0.0.1", PIPELINE_PORT, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            wait_for(
                lambda: is_server_answering(PIPELINE_BASE_URL),
                timeout=5.0,
                poll_interval=0.05,
                error_message=f"workspace server did not come up at {PIPELINE_BASE_URL}",
            )
            state.shell.start()
            try:
                yield PipelineHarness(base_url=PIPELINE_BASE_URL, broadcaster=broadcaster, registry_path=registry_path)
            finally:
                state.shell.stop()
        finally:
            server.shutdown()
            thread.join(timeout=5.0)


@pytest.fixture
def connected_client(layout_server: PipelineHarness) -> Generator[queue.Queue[str | None], None, None]:
    """One connected browser client on the default desktop: the client every op with no ``--client`` targets."""
    client_queue = layout_server.broadcaster.register()
    layout_server.broadcaster.set_client_info(client_queue, PIPELINE_CLIENT_ID, PIPELINE_DEFAULT_DESKTOP_ID)
    try:
        yield client_queue
    finally:
        layout_server.broadcaster.unregister(client_queue)
