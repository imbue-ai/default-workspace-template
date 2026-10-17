"""The real stack, inside the workspace image (docs/system/specs/screen/spec.md section 20): Xorg, PulseAudio, XFCE
and neko as subprocesses under the service, the group starting on first demand, neko answering health, a session
token minted, a screenshot taken, ``xeyes`` appearing in the window list, a member's death ending the group with its
event, and a second start bringing it back.

Skips where the env.d unit has not installed the stack (a developer laptop); the CI job that runs the suites inside
the workspace image fails on a skip, which is how a missing install is noticed."""

import io
import os
import shutil
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from flask.testing import FlaskClient

from mock_messenger_test import RecordingMessenger
from screen.connect_info import ConnectInfoAggregator
from screen.data_types import Lifecycle
from screen.events import EVENTS_RELATIVE_PATH
from screen.launch import DetachedProcessLauncher
from screen.neko_client import NekoClient
from screen.primitives import DISPLAY_SOCKET_PATH
from screen.primitives import NEKO_PROGRAM
from screen.primitives import STACK_PROGRAMS
from screen.proxy import NekoProxy
from screen.routes import build_app
from screen.service import ScreenService
from screen.stack import NEKO_BINARY_PATH
from screen.stack import ScreenStack
from screen.stack import describe_missing_install
from screen.watch import decide_group_end
from screen.watch import end_group
from screen.windows import DisplayWindowTools
from testing import SubprocessSupervisor
from testing import wait_until

_START_TIMEOUT_SECONDS = 90.0


def _skip_reason() -> str | None:
    missing = describe_missing_install()
    if missing is not None:
        return missing
    if shutil.which("xeyes") is None or shutil.which("wmctrl") is None or shutil.which("xdotool") is None:
        return "the Screen's tools (xeyes, wmctrl, xdotool) are not installed"
    if Path(DISPLAY_SOCKET_PATH).exists():
        return "display :99 is already in use on this host"
    if os.geteuid() != 0:
        return "the stack runs as root in the workspace; this user is not root"
    return None


@pytest.fixture(scope="module")
def live(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[ScreenService, FlaskClient, SubprocessSupervisor]]:
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)
    base = tmp_path_factory.mktemp("screen")
    state_dir = base / "state"
    supervisor = SubprocessSupervisor(state_dir=state_dir, log_dir=base / "logs")
    neko = NekoClient()
    service = ScreenService(
        repo_root=Path.cwd(),
        state_dir=state_dir,
        neko=neko,
        window_tools=DisplayWindowTools(),
        launcher=DetachedProcessLauncher(working_directory=Path.cwd()),
        messenger=RecordingMessenger(),
        connectivity=ConnectInfoAggregator(brokers=()),
    )
    stack = ScreenStack(
        supervisor=supervisor,
        neko=neko,
        state_dir=state_dir,
        on_lifecycle_changed=service.on_lifecycle_changed,
        on_running=service.on_running,
        set_api_token=service.set_api_token,
    )
    service.attach_stack(stack)
    service.start()
    client = build_app(service, base / "static", NekoProxy()).test_client()
    try:
        yield service, client, supervisor
    finally:
        service.shutdown("the test ended")
        supervisor.stop_everything()


def test_the_stack_starts_on_first_demand_and_neko_answers(
    live: tuple[ScreenService, FlaskClient, SubprocessSupervisor],
) -> None:
    service, client, supervisor = live
    assert client.get("/api/state").get_json()["lifecycle"] == "stopped"
    assert client.get("/").status_code == 200
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING, _START_TIMEOUT_SECONDS), (
        service.stack.stop_reason,
        [
            (program, (supervisor.log_dir / f"{program}.log").read_text()[-2000:])
            for program in STACK_PROGRAMS
            if (supervisor.log_dir / f"{program}.log").exists()
        ],
    )
    assert service.neko.is_healthy()
    assert wait_until(lambda: client.get("/api/state").get_json()["screen_size"] is not None, 30.0)
    document = client.get("/api/state").get_json()
    assert document["screen_size"] == {"width": 1920, "height": 1080, "rate": 60}
    assert document["control"]["controller"] == "nobody"
    assert client.get("/neko/health").status_code == 200


def test_a_session_is_minted_and_a_screenshot_is_a_png(
    live: tuple[ScreenService, FlaskClient, SubprocessSupervisor],
) -> None:
    _service, client, _supervisor = live
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


def test_xeyes_appears_in_the_window_list_and_a_raised_window_titles_the_screen(
    live: tuple[ScreenService, FlaskClient, SubprocessSupervisor],
) -> None:
    _service, client, _supervisor = live
    started = client.post("/api/run", json={"command": ["xeyes"]})
    assert started.status_code == 200, started.get_json()
    xeyes_pid = started.get_json()["pid"]

    def window_of(class_fragment: str) -> dict | None:
        windows = client.get("/api/windows").get_json().get("windows", [])
        return next((window for window in windows if class_fragment in window["window_class"].lower()), None)

    assert wait_until(lambda: window_of("xeyes") is not None, 30.0)
    # xeyes takes no input focus, so a focusable window is what the title follows.
    message = client.post("/api/run", json={"command": ["xmessage", "-title", "screen-test-window", "hello"]})
    assert message.status_code == 200, message.get_json()
    assert wait_until(lambda: window_of("xmessage") is not None, 30.0)
    xmessage_window = window_of("xmessage")
    assert xmessage_window is not None
    assert client.post(f"/api/windows/{xmessage_window['id']}/activate").status_code == 200
    assert wait_until(lambda: client.get("/api/state").get_json()["title"] == "screen-test-window", 10.0)
    os.kill(xeyes_pid, signal.SIGTERM)
    os.kill(message.get_json()["pid"], signal.SIGTERM)


def test_a_members_death_ends_the_group_and_records_the_event(
    live: tuple[ScreenService, FlaskClient, SubprocessSupervisor],
) -> None:
    service, client, supervisor = live
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


def test_a_second_start_brings_the_stack_back(live: tuple[ScreenService, FlaskClient, SubprocessSupervisor]) -> None:
    service, client, _supervisor = live
    assert client.post("/api/start").get_json()["lifecycle"] == "starting"
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.RUNNING, _START_TIMEOUT_SECONDS), (
        service.stack.stop_reason
    )
    assert service.neko.is_healthy()
    assert NEKO_BINARY_PATH.is_file()
