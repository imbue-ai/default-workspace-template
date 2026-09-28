import base64
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from imbue.chat.harnesses.sign_in_relay import MAX_RELAYED_BODY_BYTES
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
    def do_GET(self) -> None:
        if self.path.startswith("/callback"):
            self.send_response(302)
            self.send_header("Location", "https://platform.claude.com/oauth/code/success")
            self.end_headers()
            return
        body = b"x" * (MAX_RELAYED_BODY_BYTES + 10)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def cli_port() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CliListener)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_a_redirect_is_handed_back_rather_than_followed(cli_port: int) -> None:
    answer = fetch_loopback_callback(cli_port, "/callback?code=c&state=s")

    assert answer.status == 302
    assert answer.location == "https://platform.claude.com/oauth/code/success"


def test_a_large_answer_is_capped(cli_port: int) -> None:
    answer = fetch_loopback_callback(cli_port, "/page")

    assert answer.content_type == "text/html"
    assert len(base64.b64decode(answer.body)) == MAX_RELAYED_BODY_BYTES


def test_a_cli_that_is_not_listening_is_reported(cli_port: int) -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), _CliListener) as server:
        closed_port = server.server_address[1]

    with pytest.raises(RelayCallbackError):
        fetch_loopback_callback(closed_port, "/callback")
