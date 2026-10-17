import stat
from pathlib import Path

import yaml
from pydantic import SecretStr

from mock_neko_test import FakeNeko
from mock_supervisor_test import FakeSupervisor
from screen.data_types import Lifecycle
from screen.data_types import NekoSecrets
from screen.data_types import NekoSession
from screen.neko_config import render_neko_yaml
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
    # The service logged itself in with the admin password for its event session.
    assert fake_neko.calls == ["login:Screen service", "lock:False"]
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
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
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
        runtime_dir=tmp_path / "runtime",
        display_socket_path=tmp_path / "x11" / "X99",
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
