import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from imbue.chat.harnesses.sign_in_relay import RelayCallbackError
from imbue.chat.harnesses.sign_in_relay import fetch_loopback_callback
from imbue.chat.harnesses.sign_in_relay import is_relayable_path
from imbue.chat.harnesses.sign_in_relay import parse_relay_target
from imbue.chat.harnesses.sign_in_relay import query_state
from imbue.chat.harnesses.sign_in_relay import read_sign_in_url

_SHIM = Path(__file__).resolve().parents[5] / "scripts" / "minds_browser_shim"


def _sign_in_url(redirect_uri: str = "http%3A%2F%2Flocalhost%3A54871%2Fcallback", state: str = "s-1") -> str:
    return f"https://claude.ai/oauth/authorize?client_id=c&redirect_uri={redirect_uri}&state={state}"


def test_a_loopback_callback_is_read_off_the_sign_in_url() -> None:
    target = parse_relay_target(_sign_in_url())

    assert target is not None
    assert (target.port, target.path, target.state) == (54871, "/callback", "s-1")


def test_codexs_callback_path_is_accepted() -> None:
    target = parse_relay_target(
        "https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback&state=x"
    )

    assert target is not None
    assert (target.port, target.path) == (1455, "/auth/callback")


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(
            _sign_in_url(redirect_uri="https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"), id="remote"
        ),
        pytest.param(_sign_in_url(redirect_uri="http%3A%2F%2Fevil.example%3A54871%2Fcallback"), id="other-host"),
        pytest.param(_sign_in_url(redirect_uri="http%3A%2F%2Flocalhost%3A80%2Fcallback"), id="privileged-port"),
        pytest.param(_sign_in_url(redirect_uri="http%3A%2F%2Flocalhost%2Fcallback"), id="no-port"),
        pytest.param(_sign_in_url(redirect_uri="http%3A%2F%2Flocalhost%3A54871%2Fadmin"), id="other-path"),
        pytest.param(_sign_in_url(state=""), id="no-state"),
        pytest.param("http://claude.ai/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1%2Fcallback", id="http"),
    ],
)
def test_a_sign_in_url_this_relay_cannot_serve_names_no_target(url: str) -> None:
    assert parse_relay_target(url) is None


@pytest.mark.parametrize(
    ("path_and_query", "is_relayable"),
    [
        ("/callback?code=c&state=s", True),
        ("/", True),
        ("", False),
        ("callback", False),
        ("//evil.example/x", False),
        ("/\\evil.example", False),
        ("/" + "a" * 9000, False),
    ],
)
def test_only_a_local_path_is_relayable(path_and_query: str, is_relayable: bool) -> None:
    assert is_relayable_path(path_and_query) is is_relayable


def test_the_state_is_read_off_a_relayed_request() -> None:
    assert query_state("/callback?code=c&state=s-1") == "s-1"
    assert query_state("/callback?code=c") is None


def test_the_shim_records_the_url_it_was_asked_to_open(tmp_path: Path) -> None:
    url_file = tmp_path / "relay_url"

    completed = subprocess.run([str(_SHIM), _sign_in_url()], env={"MINDS_SIGNIN_URL_FILE": str(url_file)}, check=False)

    assert completed.returncode == 0
    assert read_sign_in_url(url_file) == _sign_in_url()


def test_a_url_still_being_written_is_not_read(tmp_path: Path) -> None:
    url_file = tmp_path / "relay_url"
    assert read_sign_in_url(url_file) is None
    url_file.write_text("https://claude.ai/oauth/auth")
    assert read_sign_in_url(url_file) is None


class _CliListener(BaseHTTPRequestHandler):
    """A sign-in CLI's loopback listener: records each request, and answers its callback with a redirect.

    `location` is where the callback redirects to, with `{port}` standing for the listener's own port.
    """

    received: list[str] = []
    location: str = ""

    def do_GET(self) -> None:
        type(self).received.append(self.path)
        if self.path.startswith("/success"):
            self.send_response(200)
        else:
            self.send_response(302)
            self.send_header("Location", self.location.format(port=self.server.server_address[1]))
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _listening(location: str) -> Iterator[tuple[int, list[str]]]:
    received: list[str] = []
    handler = type("_RecordingCliListener", (_CliListener,), {"received": received, "location": location})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], received
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("https://platform.claude.com/oauth/code/success", id="claude-provider-page"),
        pytest.param("http://localhost:1/success", id="another-loopback-port"),
        pytest.param("http://evil.example:{port}/success", id="another-host"),
    ],
)
def test_the_callback_reaches_the_cli_and_a_redirect_elsewhere_is_not_followed(location: str) -> None:
    with _listening(location) as (port, received):
        fetch_loopback_callback(port, "/callback?code=c&state=s")

    assert received == ["/callback?code=c&state=s"]


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("http://localhost:{port}/success?id_token=t", id="codex-absolute"),
        pytest.param("/success?id_token=t", id="relative"),
    ],
)
def test_a_redirect_back_to_the_same_listener_is_followed_once(location: str) -> None:
    # codex finishes its login only once its own /success page is asked for.
    with _listening(location) as (port, received):
        fetch_loopback_callback(port, "/auth/callback?code=c&state=s")

    assert received == ["/auth/callback?code=c&state=s", "/success?id_token=t"]


def test_a_cli_that_is_not_listening_is_reported() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), _CliListener) as server:
        closed_port = server.server_address[1]

    with pytest.raises(RelayCallbackError):
        fetch_loopback_callback(closed_port, "/callback")
