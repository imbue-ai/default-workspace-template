import stat
import threading
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from mock_broker_test import FakeBroker
from mock_display_modes_test import FakeDisplayModes
from mock_messenger_test import RecordingLauncher
from mock_messenger_test import RecordingMessenger
from mock_neko_test import FakeNeko
from mock_supervisor_test import FakeSupervisor
from mock_window_tools_test import FakeWindowTools
from screen.connect_info import ConnectInfoAggregator
from screen.data_types import Lifecycle
from screen.data_types import NekoSecrets
from screen.data_types import NekoSession
from screen.interfaces import ProgramState
from screen.neko_config import render_neko_yaml
from screen.primitives import STACK_PROGRAMS
from screen.service import ScreenService
from screen.stack import ScreenStack
from screen.stack import secrets_from_neko_yaml
from testing import start_service_stack
from testing import wait_until


def test_start_renders_the_configuration_and_brings_the_members_up_in_order(
    service: ScreenService, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko, tmp_path: Path
) -> None:
    start_service_stack(service)
    assert fake_supervisor.calls == [
        ("start", "screen-xorg"),
        ("start", "screen-pulse"),
        ("start", "screen-session"),
        ("start", "screen-neko"),
    ]
    state_dir = tmp_path / "state"
    assert (state_dir / "xorg.conf").read_text().startswith('Section "ServerFlags"')
    assert (state_dir / "pulse.pa").read_text().startswith("load-module module-null-sink")
    neko_yaml = state_dir / "neko.yaml"
    assert stat.S_IMODE(neko_yaml.stat().st_mode) == 0o600
    rendered = yaml.safe_load(neko_yaml.read_text())
    assert fake_neko.api_token is not None
    assert rendered["session"]["api_token"] == fake_neko.api_token.get_secret_value()
    # The service logged itself in with the admin password for its event session, and the lease's projection (a
    # free Screen is unlocked) follows on the projection thread.
    assert wait_until(lambda: fake_neko.calls == ["login:Screen service", "lock:False"]), fake_neko.calls
    assert (tmp_path / "runtime").is_dir()


def test_a_member_that_fails_to_start_stops_the_rest_and_records_why(
    service: ScreenService, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko
) -> None:
    fake_supervisor.failing_programs.add("screen-neko")
    service.ensure_started()
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.STOPPED)
    assert service.stack.stop_reason is not None
    assert "screen-neko refused to start" in service.stack.stop_reason
    assert ("stop", "screen-xorg") in fake_supervisor.calls
    assert fake_neko.api_token is None


def test_xorg_that_never_binds_the_input_drivers_socket_is_a_failed_start(
    tmp_path: Path, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko
) -> None:
    """neko dials the driver's socket once at its start, so a start waits for Xorg to bind it and gives up when it
    never does, before neko is ever started."""
    unbound = tmp_path / "never-bound.sock"
    stack = ScreenStack(
        supervisor=fake_supervisor,
        neko=fake_neko,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
        input_socket_path=unbound,
        xorg_sockets_timeout_seconds=0.2,
        install_check=lambda: None,
        on_lifecycle_changed=lambda: None,
        on_running=lambda _secrets, _session: None,
        set_api_token=fake_neko.set_api_token,
    )
    stack.ensure_started()
    assert wait_until(lambda: stack.lifecycle == Lifecycle.STOPPED)
    assert stack.stop_reason == f"the stack did not start: screen-xorg did not open {unbound}"
    assert ("start", "screen-neko") not in fake_supervisor.calls
    assert ("stop", "screen-xorg") in fake_supervisor.calls


def test_a_stale_input_socket_is_removed_before_xorg_starts(
    service: ScreenService, fake_supervisor: FakeSupervisor, tmp_path: Path
) -> None:
    """A socket a killed Xorg left behind would pass the start's wait before the new server bound its own."""
    stale = tmp_path / "runtime" / "xf86-input-neko.sock"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")
    start_service_stack(service)
    assert stale.read_text() == ""
    assert fake_supervisor.calls[0] == ("start", "screen-xorg")


def test_neko_never_answering_health_is_a_failed_start(service: ScreenService, fake_neko: FakeNeko) -> None:
    fake_neko.is_up = False
    service.ensure_started()
    assert wait_until(lambda: service.stack.lifecycle == Lifecycle.STOPPED)
    assert service.stack.stop_reason is not None
    assert "health" in service.stack.stop_reason


def test_installing_is_reported_until_the_unit_is_satisfied(
    tmp_path: Path, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko
) -> None:
    missing: list[str | None] = ["waiting for env-converge to install neko", None]
    lifecycles: list[Lifecycle] = []
    adopted: list[NekoSession] = []
    stack = ScreenStack(
        supervisor=fake_supervisor,
        neko=fake_neko,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
        input_socket_path=tmp_path / "runtime" / "xf86-input-neko.sock",
        install_check=lambda: missing.pop(0) if missing else None,
        install_poll_seconds=0.01,
        on_lifecycle_changed=lambda: lifecycles.append(stack.lifecycle),
        on_running=lambda _secrets, session: adopted.append(session),
        set_api_token=fake_neko.set_api_token,
    )
    stack.ensure_started()
    assert wait_until(lambda: stack.lifecycle == Lifecycle.RUNNING)
    # The start is visible at once, then the install wait, then the start proper.
    assert lifecycles[0] == Lifecycle.STARTING
    installing_index = lifecycles.index(Lifecycle.INSTALLING)
    assert lifecycles[installing_index + 1] == Lifecycle.STARTING
    assert lifecycles[-1] == Lifecycle.RUNNING
    assert len(adopted) == 1 and adopted[0].is_admin is True


# The starter is meant to die of the planted error, which pytest would otherwise report as an unhandled thread exception.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_starter_that_dies_unexpectedly_does_not_pin_the_lifecycle_at_starting(
    tmp_path: Path, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko
) -> None:
    checks: list[str | None] = ["first"]

    def install_check() -> str | None:
        if checks:
            checks.pop()
            raise ZeroDivisionError("a bug in the start")
        return None

    stack = ScreenStack(
        supervisor=fake_supervisor,
        neko=fake_neko,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
        input_socket_path=tmp_path / "runtime" / "xf86-input-neko.sock",
        install_check=install_check,
        on_lifecycle_changed=lambda: None,
        on_running=lambda _secrets, _session: None,
        set_api_token=fake_neko.set_api_token,
    )
    assert stack.ensure_started() == Lifecycle.STARTING
    starter = stack._starter
    assert starter is not None
    starter.join(timeout=5.0)
    assert stack.lifecycle == Lifecycle.STARTING
    assert stack.ensure_started() == Lifecycle.STARTING
    assert wait_until(lambda: stack.lifecycle == Lifecycle.RUNNING), stack.stop_reason


def test_shutdown_ends_a_stack_still_waiting_for_its_install(
    tmp_path: Path, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko, recording_messenger: RecordingMessenger
) -> None:
    service = ScreenService(
        repo_root=tmp_path,
        state_dir=tmp_path / "state",
        neko=fake_neko,
        window_tools=FakeWindowTools(),
        display_modes=FakeDisplayModes(),
        launcher=RecordingLauncher(),
        messenger=recording_messenger,
        connectivity=ConnectInfoAggregator(
            brokers=(FakeBroker(name="desktop"), FakeBroker(name="share gateway")), deadline_seconds=1.0
        ),
        is_event_subscription_enabled=False,
        is_clipboard_monitor_enabled=False,
    )
    stack = ScreenStack(
        supervisor=fake_supervisor,
        neko=fake_neko,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
        input_socket_path=tmp_path / "runtime" / "xf86-input-neko.sock",
        install_check=lambda: "waiting for env-converge to install neko",
        install_poll_seconds=0.01,
        on_lifecycle_changed=service.on_lifecycle_changed,
        on_running=service.on_running,
        set_api_token=service.set_api_token,
    )
    service.attach_stack(stack)
    service.start()
    service.ensure_started()
    assert wait_until(lambda: stack.lifecycle == Lifecycle.INSTALLING)
    service.shutdown("the Screen service stopped")
    starter = stack._starter
    assert starter is not None
    starter.join(timeout=5.0)
    assert not starter.is_alive()
    assert stack.lifecycle == Lifecycle.STOPPED
    assert stack.stop_reason == "the Screen service stopped"


def test_stop_ends_the_members_neko_first(service: ScreenService, fake_supervisor: FakeSupervisor) -> None:
    start_service_stack(service)
    service.stack.stop("quit")
    assert fake_supervisor.calls[-4:] == [
        ("stop", "screen-neko"),
        ("stop", "screen-session"),
        ("stop", "screen-pulse"),
        ("stop", "screen-xorg"),
    ]
    assert service.stack.lifecycle == Lifecycle.STOPPED
    assert service.stack.stop_reason == "quit"


def _become_healthy_after(fake_neko: FakeNeko, delay_seconds: float) -> None:
    fake_neko.is_up = False
    threading.Timer(delay_seconds, lambda: setattr(fake_neko, "is_up", True)).start()


def test_a_stack_can_be_started_again_after_a_stop(service: ScreenService, fake_neko: FakeNeko) -> None:
    # neko takes a moment to answer its health route, as the real one does, so a start has to wait for it.
    _become_healthy_after(fake_neko, 0.2)
    start_service_stack(service)
    service.stack.stop("quit")
    assert service.stack.lifecycle == Lifecycle.STOPPED
    _become_healthy_after(fake_neko, 0.2)
    start_service_stack(service)


def test_a_stop_during_a_start_abandons_it_and_keeps_the_stops_reason(
    service: ScreenService, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko
) -> None:
    fake_neko.is_up = False
    service.ensure_started()
    assert wait_until(lambda: ("start", "screen-neko") in fake_supervisor.calls)
    service.stack.stop("quit")
    starter = service.stack._starter
    assert starter is not None
    starter.join(timeout=5.0)
    assert not starter.is_alive()
    assert service.stack.lifecycle == Lifecycle.STOPPED
    assert service.stack.stop_reason == "quit"
    assert fake_neko.api_token is None
    assert all(fake_supervisor.program_state(program) == ProgramState.STOPPED for program in STACK_PROGRAMS)
    # The abandoned start left nothing behind that keeps a fresh one from succeeding.
    fake_neko.is_up = True
    start_service_stack(service)


def test_a_running_stack_is_adopted_from_its_neko_yaml(
    service: ScreenService, fake_supervisor: FakeSupervisor, fake_neko: FakeNeko, tmp_path: Path
) -> None:
    start_service_stack(service)
    token_before = fake_neko.api_token
    # A second service life over the same supervisord and neko.
    second = ScreenStack(
        supervisor=fake_supervisor,
        neko=fake_neko,
        state_dir=tmp_path / "state",
        home=tmp_path / "home",
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
        input_socket_path=tmp_path / "runtime" / "xf86-input-neko.sock",
        install_check=lambda: None,
        on_lifecycle_changed=lambda: None,
        on_running=lambda _secrets, _session: None,
        set_api_token=fake_neko.set_api_token,
    )
    assert second.adopt_running_stack() is True
    assert second.lifecycle == Lifecycle.RUNNING
    assert fake_neko.api_token == token_before
    fake_supervisor.stop_program("screen-neko")
    third = second.model_copy()
    assert third.adopt_running_stack() is False


def test_secrets_are_read_back_from_a_rendered_neko_yaml() -> None:
    secrets = NekoSecrets(user_password=SecretStr("u"), admin_password=SecretStr("a"), api_token=SecretStr("t"))
    read_back = secrets_from_neko_yaml(render_neko_yaml(secrets))
    assert read_back.user_password.get_secret_value() == "u"
    assert read_back.admin_password.get_secret_value() == "a"
    assert read_back.api_token.get_secret_value() == "t"
