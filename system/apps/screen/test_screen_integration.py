"""The real stack, inside the workspace image (docs/system/specs/screen/spec.md section 20): Xorg, PulseAudio, XFCE
and neko as subprocesses under the service, the group starting on first demand, neko answering health, a session
token minted, a screenshot taken, ``xeyes`` appearing in the window list, the browser launch path putting a real
Fortress on the Screen with its tab checkpointed, a member's death ending the group with its event, and a second
start bringing it back.

Skips where the env.d unit has not installed the stack (a developer laptop); the CI job that runs the suites inside
the workspace image fails on a skip, which is how a missing install is noticed. That job spreads a module's tests over
several sandboxes, so no test may lean on an earlier one having started the stack: each takes the ``running`` fixture,
or, as the first-demand test does, begins a fresh life and starts the stack itself. The root pytest config's ten-second
timeout is for unit tests, so the module sets its own: a stack start waits up to 90 seconds and a Fortress launch up to
two minutes."""

import io
import json
import os
import shutil
import signal
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from flask.testing import FlaskClient
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from mock_messenger_test import RecordingMessenger
from screen.browser_fleet import BrowserFleet
from screen.browser_manifest import ManifestStore
from screen.cdp_client import WebSocketCdpConnector
from screen.cdp_proxy import ProxyServer
from screen.chrome_launcher import SubprocessChromiumLauncher
from screen.connect_info import ConnectInfoAggregator
from screen.data_types import Lifecycle
from screen.events import EVENTS_RELATIVE_PATH
from screen.interfaces import ProgramState
from screen.launch import DetachedProcessLauncher
from screen.launch import screen_process_environment
from screen.neko_client import NekoClient
from screen.primitives import BrowserName
from screen.primitives import DISPLAY_SOCKET_PATH
from screen.primitives import FORTRESS_EXECUTABLE
from screen.primitives import NEKO_PROGRAM
from screen.primitives import STACK_PROGRAMS
from screen.proxy import NekoProxy
from screen.routes import build_app
from screen.service import ScreenService
from screen.sizing import XrandrDisplayModes
from screen.stack import NEKO_BINARY_PATH
from screen.stack import ScreenStack
from screen.stack import describe_missing_browser_install
from screen.stack import describe_missing_install
from screen.watch import decide_group_end
from screen.watch import end_group
from screen.windows import DisplayWindowTools
from testing import SubprocessSupervisor
from testing import wait_until

pytestmark = pytest.mark.timeout(300)

_START_TIMEOUT_SECONDS = 90.0


def _skip_reason() -> str | None:
    missing = describe_missing_install()
    if missing is not None:
        return missing
    if any(shutil.which(tool) is None for tool in ("xeyes", "xfce4-terminal", "wmctrl", "xdotool")):
        return "the Screen's tools (xeyes, xfce4-terminal, wmctrl, xdotool) are not installed"
    if Path(DISPLAY_SOCKET_PATH).exists():
        return "display :99 is already in use on this host"
    if os.geteuid() != 0:
        return "the stack runs as root in the workspace; this user is not root"
    return None


class _LiveService(FrozenModel):
    """One life of the Screen service over the real programs, as ``screen.main`` wires it."""

    model_config = {"arbitrary_types_allowed": True}

    service: ScreenService = Field(description="The service")
    client: FlaskClient = Field(description="Its routes")
    supervisor: SubprocessSupervisor = Field(description="The programs, as the service's supervisor sees them")
    proxy_server: ProxyServer = Field(description="The agents' CDP proxy, bound to a free port")

    def shutdown(self, reason: str) -> None:
        self.service.shutdown(reason)
        self.proxy_server.stop()

    def program_logs(self) -> list[tuple[str, str]]:
        """The tail of every member's output, for the message of a start that did not come up."""
        return [
            (program, (self.supervisor.log_dir / f"{program}.log").read_text()[-2000:])
            for program in STACK_PROGRAMS
            if (self.supervisor.log_dir / f"{program}.log").exists()
        ]

    def wait_until_running(self) -> None:
        assert wait_until(lambda: self.service.stack.lifecycle == Lifecycle.RUNNING, _START_TIMEOUT_SECONDS), (
            self.service.stack.stop_reason,
            self.program_logs(),
        )


class _LiveServices(MutableModel):
    """The module's service lives over one supervisor: at most one at a time, built on demand and replaced after a
    Quit, so a test that ends the service leaves the next one a life to start the stack in."""

    model_config = {"arbitrary_types_allowed": True}

    base: Path = Field(frozen=True, description="The state dir, the browser's manifest and profile")
    supervisor: SubprocessSupervisor = Field(frozen=True, description="The programs, shared by every life")
    current: _LiveService | None = Field(default=None, description="The life that is up, if any")

    def ensure_service(self) -> _LiveService:
        if self.current is None:
            self.current = _build_live_service(self.base, self.supervisor)
        return self.current

    def shutdown(self, reason: str) -> None:
        """End the current life (if any) and wait for its X server to take its socket with it, so the next life's
        start finds a free display rather than a stale socket."""
        if self.current is None:
            return
        self.current.shutdown(reason)
        self.current = None
        assert wait_until(lambda: not Path(DISPLAY_SOCKET_PATH).exists(), 10.0)


def _build_live_service(base: Path, supervisor: SubprocessSupervisor) -> _LiveService:
    """Build and start a service over ``base`` (the state dir, the browser's manifest and profile) the way
    ``screen.main`` does, with the fleet attached before the stack so a stop checkpoints Chromium's tabs."""
    state_dir = base / "state"
    neko = NekoClient()
    service = ScreenService(
        repo_root=Path.cwd(),
        state_dir=state_dir,
        neko=neko,
        window_tools=DisplayWindowTools(),
        display_modes=XrandrDisplayModes(),
        launcher=DetachedProcessLauncher(working_directory=Path.cwd()),
        is_clipboard_monitor_enabled=False,
        messenger=RecordingMessenger(),
        connectivity=ConnectInfoAggregator(brokers=()),
    )
    proxy_server = ProxyServer(requested_port=0)
    proxy_server.start()
    fleet = BrowserFleet(
        launcher=SubprocessChromiumLauncher(),
        cdp_connector=WebSocketCdpConnector(),
        proxy_server=proxy_server,
        manifest_store=ManifestStore(path=base / "apps" / "browser" / "instances.json"),
        profile_root=base / "profiles",
        executable=FORTRESS_EXECUTABLE,
        install_check=describe_missing_browser_install,
        environment=screen_process_environment,
        is_sandbox_disabled=True,
        control=service.control,
        on_changed=service.broadcast_state,
        on_agent_activity=service.touch,
        on_chromium_processes_expected=lambda: None,
        on_browser_running=service.on_browser_running,
    )
    service.attach_fleet(fleet, band_sweeper=None)
    stack = ScreenStack(
        supervisor=supervisor,
        neko=neko,
        state_dir=state_dir,
        on_lifecycle_changed=service.on_lifecycle_changed,
        on_running=service.on_running,
        set_api_token=service.set_api_token,
        on_stopping=fleet.prepare_for_stack_stop,
    )
    service.attach_stack(stack)
    service.start()
    client = build_app(service, base / "static", NekoProxy()).test_client()
    return _LiveService(service=service, client=client, supervisor=supervisor, proxy_server=proxy_server)


@pytest.fixture(scope="module")
def live_base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)
    return tmp_path_factory.mktemp("screen")


@pytest.fixture(scope="module")
def live_supervisor(live_base: Path) -> Iterator[SubprocessSupervisor]:
    supervisor = SubprocessSupervisor(state_dir=live_base / "state", log_dir=live_base / "logs")
    try:
        yield supervisor
    finally:
        supervisor.stop_everything()


@pytest.fixture(scope="module")
def live(live_base: Path, live_supervisor: SubprocessSupervisor) -> Iterator[_LiveServices]:
    lives = _LiveServices(base=live_base, supervisor=live_supervisor)
    try:
        yield lives
    finally:
        lives.shutdown("the test ended")


@pytest.fixture
def running(live: _LiveServices) -> _LiveService:
    """The current service life with its stack running, started on demand as a page load starts it."""
    built = live.ensure_service()
    built.service.ensure_started()
    built.wait_until_running()
    return built


def test_the_stack_starts_on_first_demand_and_neko_answers(live: _LiveServices) -> None:
    # A life of its own, so the stack is down until the page load whatever ran before.
    live.shutdown("a fresh life for the first demand")
    built = live.ensure_service()
    service, client = built.service, built.client
    assert client.get("/api/state").get_json()["lifecycle"] == "stopped"
    assert client.get("/").status_code == 200
    built.wait_until_running()
    assert service.neko.is_healthy()
    assert wait_until(lambda: client.get("/api/state").get_json()["screen_size"] is not None, 30.0)
    document = client.get("/api/state").get_json()
    assert document["screen_size"] == {"width": 1920, "height": 1080, "rate": 60}
    assert document["control"]["controller"] == "nobody"
    assert client.get("/neko/health").status_code == 200


def test_a_session_is_minted_and_a_screenshot_is_a_png(running: _LiveService) -> None:
    client = running.client
    session = client.post("/api/session", headers={"X-Imbue-Identity": '{"owner": true, "email": "josh@imbue.com"}'})
    assert session.status_code == 200, session.get_json()
    assert session.get_json()["name"] == "josh"
    whoami = client.get("/neko/api/whoami", headers={"Authorization": f"Bearer {session.get_json()['token']}"})
    assert whoami.status_code == 200
    assert whoami.get_json()["profile"]["name"] == "josh"
    shot = client.get("/api/screenshot?quality=80")
    assert shot.status_code == 200
    with Image.open(io.BytesIO(shot.data)) as image:
        assert image.format == "PNG"
        assert image.size == (1920, 1080)


def test_xeyes_appears_in_the_window_list_and_a_raised_window_titles_the_screen(running: _LiveService) -> None:
    client = running.client
    started = client.post("/api/run", json={"command": ["xeyes"]})
    assert started.status_code == 200, started.get_json()
    xeyes_pid = started.get_json()["pid"]

    def window_of(class_fragment: str) -> dict | None:
        windows = client.get("/api/windows").get_json().get("windows", [])
        return next((window for window in windows if class_fragment in window["window_class"].lower()), None)

    assert wait_until(lambda: window_of("xeyes") is not None, 30.0)
    # xeyes takes no input focus, so a focusable window is what the title follows: a terminal of its own process
    # (the image has no xmessage) with a fixed title.
    terminal = client.post(
        "/api/run", json={"command": ["xfce4-terminal", "--disable-server", "-T", "screen-test-window"]}
    )
    assert terminal.status_code == 200, terminal.get_json()
    assert wait_until(lambda: window_of("xfce4-terminal") is not None, 30.0)
    terminal_window = window_of("xfce4-terminal")
    assert terminal_window is not None
    assert client.post(f"/api/windows/{terminal_window['id']}/activate").status_code == 200
    assert wait_until(lambda: client.get("/api/state").get_json()["title"] == "screen-test-window", 10.0)
    os.kill(xeyes_pid, signal.SIGTERM)
    os.kill(terminal.get_json()["pid"], signal.SIGTERM)
    assert wait_until(lambda: window_of("xfce4-terminal") is None, 10.0)


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"<html><head><title>screen-browser-test</title></head><body>hello</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.mark.browser
def test_the_browser_launch_path_puts_fortress_on_the_screen_and_checkpoints_its_tab(running: _LiveService) -> None:
    service, client = running.service, running.client
    missing = describe_missing_browser_install()
    if missing is not None:
        pytest.skip(missing)
    page_server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    serving = threading.Thread(target=page_server.serve_forever, daemon=True)
    serving.start()
    try:
        page_url = f"http://127.0.0.1:{page_server.server_address[1]}/start"
        answer = client.post("/launch/browser", json={"url": page_url})
        assert answer.status_code == 200, answer.get_json()
        assert answer.get_json() == {"path": "/"}
        browser = service.fleet.get(BrowserName("browser-1"))
        assert wait_until(lambda: browser.is_running, 120.0), browser.lifecycle

        def chromium_window() -> dict | None:
            windows = client.get("/api/windows").get_json().get("windows", [])
            return next(
                (w for w in windows if any(m in w["window_class"].lower() for m in ("tilion", "chrome", "chromium"))),
                None,
            )

        assert wait_until(lambda: chromium_window() is not None, 60.0)
        assert wait_until(lambda: client.get("/api/state").get_json()["title"] == "Browser", 30.0)
        listed = client.get("/browsers").get_json()["browsers"][0]
        assert listed["lifecycle"] == "running"
        assert wait_until(
            lambda: any(tab["url"] == page_url for tab in client.get("/browsers").get_json()["browsers"][0]["tabs"]),
            30.0,
        )
        attach = client.get("/browsers/browser-1/attach", headers={"X-Mngr-Agent-Id": "chat-live"}).get_json()
        assert attach["ok"] is True
        # The proxy answers Chromium's discovery with its own URL in place of the real port.
        version = json.loads(urllib.request.urlopen(attach["attach_url"] + "/json/version/", timeout=10).read())
        assert version["webSocketDebuggerUrl"].startswith("ws://127.0.0.1:")
        assert version["webSocketDebuggerUrl"].endswith(attach["attach_url"].rsplit("/", 1)[1])
        service.fleet.checkpoint()
        manifest = service.fleet.manifest_store.read()
        assert manifest is not None
        assert manifest.browsers[0].tabs == (page_url,)
        assert client.post("/browsers/browser-1/stop").get_json() == {"stopped": True}
        assert wait_until(lambda: chromium_window() is None, 30.0)
        assert client.get("/api/state").get_json()["chromium"]["state"] == "stopped"
    finally:
        page_server.shutdown()
        page_server.server_close()


def test_a_members_death_ends_the_group_and_records_the_event(running: _LiveService) -> None:
    service, client, supervisor = running.service, running.client, running.supervisor
    supervisor.kill_program(NEKO_PROGRAM)
    payload = f"processname:{NEKO_PROGRAM} groupname:screen-stack from_state:RUNNING expected:0 pid:1"
    notified: list[int] = []
    end_group(
        decide_group_end("PROCESS_STATE_EXITED", payload),
        supervisor,
        service.state_dir,
        lambda stopped: notified.append(client.post("/api/internal/stopped", json=stopped.model_dump()).status_code),
    )
    assert notified == [204]
    assert service.stack.lifecycle == Lifecycle.STOPPED
    assert service.stack.stop_reason == "screen-neko exited unexpectedly"
    assert not service.stack.is_any_member_running()
    lines = (service.state_dir / EVENTS_RELATIVE_PATH).read_text().splitlines()
    assert len(lines) == 1
    assert '"type": "screen_stopped"' in lines[0]
    assert not Path(DISPLAY_SOCKET_PATH).exists() or wait_until(lambda: not Path(DISPLAY_SOCKET_PATH).exists(), 10.0)


def test_a_second_start_brings_the_stack_back(running: _LiveService) -> None:
    service, client = running.service, running.client
    service.stack.stop("the test ended the first life of the stack")
    assert service.stack.lifecycle == Lifecycle.STOPPED
    assert not service.stack.is_any_member_running()
    assert client.post("/api/start").get_json()["lifecycle"] == "starting"
    running.wait_until_running()
    assert service.neko.is_healthy()
    assert NEKO_BINARY_PATH.is_file()


def test_quit_ends_every_member_and_the_next_open_starts_a_fresh_stack(
    live: _LiveServices, running: _LiveService
) -> None:
    """Quit stops the service, whose shutdown ends the group (spec section 9.3); the next open of the window finds
    a restarted service and a page load starts the stack again."""
    service, supervisor = running.service, running.supervisor
    assert service.stack.lifecycle == Lifecycle.RUNNING
    live.shutdown("Quit from the window menu")
    assert service.stack.lifecycle == Lifecycle.STOPPED
    assert service.stack.stop_reason == "Quit from the window menu"
    assert not service.stack.is_any_member_running()
    assert all(supervisor.program_state(program) != ProgramState.RUNNING for program in STACK_PROGRAMS)

    restarted = live.ensure_service()
    assert restarted.client.get("/api/state").get_json()["lifecycle"] == "stopped"
    assert restarted.client.get("/").status_code == 200
    restarted.wait_until_running()
    assert restarted.service.neko.is_healthy()
    assert restarted.client.get("/api/state").get_json()["control"]["controller"] == "nobody"
    live.shutdown("the test ended")
