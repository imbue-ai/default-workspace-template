"""The ChatGPT sign-ins the flow service runs through a short-lived `codex app-server`."""

import itertools
import threading
from pathlib import Path

import pytest

from imbue.chat.accounts import read_index
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.auth_flows import FlowError
from imbue.chat.harnesses.auth_flows import FlowShape
from imbue.chat.harnesses.auth_flows import FlowState
from imbue.chat.harnesses.codex.sign_in import app_server_argv
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.testing import FakePexpectProcess
from imbue.chat.testing import wait_until_true
from imbue.mngr_codex.app_server_client import ChatgptDeviceLoginStart
from imbue.mngr_codex.app_server_client import ChatgptLoginStart
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import LoginCompleted

_AUTH_URL = (
    "https://auth.openai.com/oauth/authorize?response_type=code&client_id=app_x"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&state=codex-state"
)


class _ScriptedLoginClient:
    """Answers the login RPCs the way a real app-server does, and finishes when the test says so."""

    def __init__(self, outcome: LoginCompleted | None) -> None:
        self.outcome = outcome
        self.finished = threading.Event()
        self.is_closed = False

    def start_chatgpt_login(self) -> ChatgptLoginStart:
        return ChatgptLoginStart.model_validate({"loginId": "login-1", "authUrl": _AUTH_URL})

    def start_device_login(self) -> ChatgptDeviceLoginStart:
        return ChatgptDeviceLoginStart.model_validate(
            {"loginId": "login-2", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "ABCD-1234"}
        )

    def wait_login_completed(self, login_id: str, timeout_seconds: float) -> LoginCompleted:
        self.finished.wait(timeout_seconds)
        if self.is_closed or self.outcome is None:
            raise CodexAppServerError("app-server websocket connection closed")
        return self.outcome

    def close(self) -> None:
        self.is_closed = True
        self.finished.set()


def _service(tmp_path: Path, client: _ScriptedLoginClient, spawned: list[list[str]] | None = None) -> AuthFlowService:
    def spawner(_binary: str, args: list[str], *_a: object, **_k: object) -> FakePexpectProcess:
        if spawned is not None:
            spawned.append(args)
        # What the real app-server does first: listen on the socket it was given.
        Path(args[-1].removeprefix("unix://")).touch()
        return FakePexpectProcess([(0, "")])

    ticks = itertools.count(start=0.0, step=10.0)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return AuthFlowService.create(
        home=tmp_path,
        work_dir=work_dir,
        spawner=spawner,
        probe=lambda *_a: SignedIn.YES,
        login_client_connector=lambda _socket: client,
        fetch_callback=lambda _port, _path: None,
        # Moves on each read, so a flow still pending after a relayed callback is reported within a
        # few polls rather than after the real wait.
        clock=lambda: next(ticks),
    )


def test_the_browser_login_offers_codexs_page_for_relaying(tmp_path: Path) -> None:
    spawned: list[list[str]] = []
    service = _service(tmp_path, _ScriptedLoginClient(None), spawned)

    started = service.start("openai", "chatgpt")

    assert started.shape is FlowShape.BROWSER
    assert started.url == _AUTH_URL
    assert started.relay_url == _AUTH_URL
    assert spawned[0][:2] == ["app-server", "--listen"]
    service.abort(started.flow_id)


def test_codex_saying_the_login_succeeded_signs_the_account_in(tmp_path: Path) -> None:
    client = _ScriptedLoginClient(LoginCompleted(success=True))
    service = _service(tmp_path, client)
    started = service.start("openai", "chatgpt")
    assert service.poll(started.flow_id).state is FlowState.PENDING

    client.finished.set()
    wait_until_true(lambda: service.poll(started.flow_id).state is not FlowState.PENDING, 5.0, "the login settling")

    assert service.poll(started.flow_id).state is FlowState.OK
    (account,) = read_index(tmp_path).accounts
    assert account.lane == "openai"
    assert client.is_closed


def test_codex_saying_the_login_failed_fails_the_flow(tmp_path: Path) -> None:
    client = _ScriptedLoginClient(LoginCompleted(success=False, error="access_denied"))
    service = _service(tmp_path, client)
    started = service.start("openai", "chatgpt")

    client.finished.set()
    wait_until_true(lambda: service.poll(started.flow_id).state is not FlowState.PENDING, 5.0, "the login settling")

    status = service.poll(started.flow_id)
    assert status.state is FlowState.FAILED
    assert status.detail == "ChatGPT didn't finish the sign-in: access_denied"
    assert read_index(tmp_path).accounts == ()


def test_the_device_login_shows_its_page_and_code(tmp_path: Path) -> None:
    service = _service(tmp_path, _ScriptedLoginClient(None))

    started = service.start("openai", "device")

    assert started.shape is FlowShape.CODE_THEN_WAIT
    assert (started.url, started.code) == ("https://auth.openai.com/codex/device", "ABCD-1234")
    assert started.relay_url is None
    service.abort(started.flow_id)


def test_abandoning_a_codex_sign_in_closes_its_app_server(tmp_path: Path) -> None:
    client = _ScriptedLoginClient(None)
    service = _service(tmp_path, client)
    started = service.start("openai", "chatgpt")

    service.abort(started.flow_id)

    assert client.is_closed
    assert read_index(tmp_path).accounts == ()


def test_the_browser_logins_callback_is_relayed_to_codexs_listener(tmp_path: Path) -> None:
    service = _service(tmp_path, _ScriptedLoginClient(None))
    started = service.start("openai", "chatgpt")

    status = service.relay_callback(started.flow_id, "/auth/callback?code=c&state=codex-state")

    # Codex has not said how its login ended, so the flow is still finishing.
    assert status.state is FlowState.PENDING
    service.abort(started.flow_id)


def test_a_device_login_takes_no_pasted_code(tmp_path: Path) -> None:
    service = _service(tmp_path, _ScriptedLoginClient(None))
    started = service.start("openai", "device")

    with pytest.raises(FlowError):
        service.submit_code(started.flow_id, "ABCD-1234")
    service.abort(started.flow_id)


def test_the_app_server_listens_on_the_socket_it_is_given() -> None:
    assert app_server_argv(Path("/tmp/x/codex.sock")) == ["app-server", "--listen", "unix:///tmp/x/codex.sock"]
