import hashlib
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic import SecretBytes

from mock_broker_test import FakeBroker
from mock_chromium_test import FakeCdpConnector
from mock_chromium_test import FakeChromiumLauncher
from mock_display_modes_test import FakeDisplayModes
from mock_messenger_test import RecordingLauncher
from mock_messenger_test import RecordingMessenger
from mock_neko_test import FakeNeko
from mock_supervisor_test import FakeSupervisor
from mock_window_tools_test import FakeWindowTools
from screen.browser_fleet import BrowserFleet
from screen.browser_manifest import ManifestStore
from screen.cdp_proxy import ProxyServer
from screen.connect_info import ConnectInfoAggregator
from screen.data_types import DisplaySettings
from screen.owner_gate import OwnerGate
from screen.proxy import NekoProxy
from screen.routes import build_app
from screen.service import ScreenService
from screen.stack import ScreenStack
from screen.xclipboard import XClipboard
from testing import ManualClock
from workspace_layout.shell_url import ENV_SHELL_URL
from workspace_layout.testing import FAKE_WINDOW_ID
from workspace_layout.testing import LoopbackShell
from workspace_layout.testing import desktop_answer
from workspace_layout.testing import fake_desktop


@pytest.fixture
def fake_supervisor(tmp_path: Path) -> FakeSupervisor:
    return FakeSupervisor(
        display_socket_path=tmp_path / "x11" / "X99", input_socket_path=tmp_path / "runtime" / "xf86-input-neko.sock"
    )


@pytest.fixture
def fake_neko() -> FakeNeko:
    return FakeNeko()


@pytest.fixture
def recording_messenger() -> RecordingMessenger:
    return RecordingMessenger()


@pytest.fixture
def fake_display_modes() -> FakeDisplayModes:
    return FakeDisplayModes()


@pytest.fixture
def manual_clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def fake_xclip(tmp_path: Path) -> Path:
    """A stand-in ``xclip``: ``-i`` records its target and stdin under the record directory, ``-o`` answers the
    targets and payloads the test wrote there as ``out.<target>`` (a slash in the target spelled ``_``)."""
    record_dir = tmp_path / "xclip-record"
    record_dir.mkdir()
    script = tmp_path / "xclip"
    script.write_text(f"""#!/usr/bin/env bash
set -eu
record="{record_dir}"
target=""
mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) target="$2"; shift 2 ;;
    -i) mode=in; shift ;;
    -o) mode=out; shift ;;
    *) shift ;;
  esac
done
if [ "$mode" = in ]; then
  printf '%s' "$target" > "$record/set.target"
  cat > "$record/set.bytes"
  exit 0
fi
if [ -f "$record/out.${{target//\\//_}}" ]; then
  cat "$record/out.${{target//\\//_}}"
  exit 0
fi
exit 1
""")
    script.chmod(0o755)
    return script


@pytest.fixture
def fake_brokers() -> tuple[FakeBroker, FakeBroker]:
    """The desktop and the share gateway as the service's connectivity brokers, answering nothing until a test says."""
    return (FakeBroker(name="desktop"), FakeBroker(name="share gateway"))


@pytest.fixture
def fake_chromium_launcher() -> FakeChromiumLauncher:
    return FakeChromiumLauncher()


@pytest.fixture
def fake_cdp_connector() -> FakeCdpConnector:
    return FakeCdpConnector()


@pytest.fixture
def proxy_server() -> ProxyServer:
    """The agents' CDP proxy, registered against but never bound (a test that needs the socket starts it itself)."""
    return ProxyServer(requested_port=0)


@pytest.fixture
def display_settings() -> DisplaySettings:
    """The ``service`` fixture's display settings, a workspace's by default; a test parametrizes this name to fix the
    size or change the codec."""
    return DisplaySettings()


@pytest.fixture
def service(
    tmp_path: Path,
    display_settings: DisplaySettings,
    fake_supervisor: FakeSupervisor,
    fake_neko: FakeNeko,
    fake_display_modes: FakeDisplayModes,
    recording_messenger: RecordingMessenger,
    manual_clock: ManualClock,
    fake_xclip: Path,
    fake_brokers: tuple[FakeBroker, FakeBroker],
    fake_chromium_launcher: FakeChromiumLauncher,
    fake_cdp_connector: FakeCdpConnector,
    proxy_server: ProxyServer,
) -> Iterator[ScreenService]:
    """A service over fakes whose stack starts at once, with no event subscription and no clipboard monitor, on a
    clock the test moves, with a recording xclip, a desktop folder under the test's directory and a browser fleet
    whose Chromium is in memory."""
    built = ScreenService(
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        neko=fake_neko,
        window_tools=FakeWindowTools(),
        display_modes=fake_display_modes,
        clipboard=XClipboard(display=":4242", xclip_command=(str(fake_xclip),)),
        desktop_dir=tmp_path / "Desktop",
        launcher=RecordingLauncher(),
        messenger=recording_messenger,
        connectivity=ConnectInfoAggregator(brokers=fake_brokers, deadline_seconds=1.0),
        display_settings=display_settings,
        is_event_subscription_enabled=False,
        is_clipboard_monitor_enabled=False,
        clock=manual_clock,
    )
    fleet = BrowserFleet(
        launcher=fake_chromium_launcher,
        cdp_connector=fake_cdp_connector,
        proxy_server=proxy_server,
        manifest_store=ManifestStore(path=tmp_path / "apps" / "browser" / "instances.json"),
        profile_root=tmp_path / "profiles",
        executable=tmp_path / "fortress" / "tilion",
        install_check=lambda: None,
        environment=lambda: {"DISPLAY": ":4242"},
        is_sandbox_disabled=True,
        control=built.control,
        on_changed=built.broadcast_state,
        on_agent_activity=built.touch,
        on_chromium_processes_expected=lambda: None,
        on_browser_running=built.on_browser_running,
    )
    built.attach_fleet(fleet, band_sweeper=None)
    stack = ScreenStack(
        supervisor=fake_supervisor,
        neko=fake_neko,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        desktop_dir=tmp_path / "Desktop",
        download_dir=tmp_path / "Downloads",
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
        input_socket_path=tmp_path / "runtime" / "xf86-input-neko.sock",
        install_check=lambda: None,
        neko_health_timeout_seconds=1.0,
        install_poll_seconds=0.01,
        on_lifecycle_changed=built.on_lifecycle_changed,
        on_running=built.on_running,
        set_api_token=built.set_api_token,
        on_stopping=fleet.prepare_for_stack_stop,
        display_settings=display_settings,
    )
    built.attach_stack(stack)
    built.start()
    yield built
    built.shutdown("the test ended")


@pytest.fixture
def loopback_shell(monkeypatch: pytest.MonkeyPatch) -> Iterator[LoopbackShell]:
    """A shell on loopback that answers every op as a raised window on the home desktop, named to the service."""
    with LoopbackShell(
        op_answer={**desktop_answer(fake_desktop("home"), "c1", FAKE_WINDOW_ID), "shown": "raised"}
    ) as shell:
        monkeypatch.setenv(ENV_SHELL_URL, shell.url)
        yield shell


@pytest.fixture
def app(service: ScreenService, tmp_path: Path) -> Flask:
    return build_app(service, tmp_path / "static", NekoProxy(), None)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture
def owner_token() -> str:
    return "screen-owner-token-" + uuid4().hex


@pytest.fixture
def owner_token_sha256_path(tmp_path: Path, owner_token: str) -> Path:
    """The claimed sandbox's digest file, with a trailing newline as a shell's write leaves one."""
    path = tmp_path / "screen_token_sha256"
    path.write_text(hashlib.sha256(owner_token.encode("utf-8")).hexdigest() + "\n")
    return path


@pytest.fixture
def owner_gate(owner_token_sha256_path: Path) -> OwnerGate:
    return OwnerGate(
        owner_token_sha256_path=owner_token_sha256_path,
        frame_ancestors="https://chrome.example.test",
        session_secret=SecretBytes(uuid4().bytes),
    )


@pytest.fixture
def gated_app(service: ScreenService, tmp_path: Path, owner_gate: OwnerGate) -> Flask:
    """The service's routes in gate mode (an auth sandbox's), with a neko proxy pointed at a port nothing serves."""
    return build_app(
        service,
        tmp_path / "static",
        NekoProxy(base_url="http://127.0.0.1:9", websocket_url="ws://127.0.0.1:9"),
        owner_gate,
    )


@pytest.fixture
def gated_client(gated_app: Flask) -> FlaskClient:
    """A client that keeps no cookies, so each test names the cookies a request carries."""
    return gated_app.test_client(use_cookies=False)
