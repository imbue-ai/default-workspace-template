"""A relayed Claude sign-in from end to end, against a stand-in for the `claude` CLI.

The stand-in does what `claude auth login` does on the wire: it listens on a loopback port, runs
`$BROWSER` with an authorize URL whose `redirect_uri` is that port, and prints "Login successful"
once the callback carrying its `state` arrives. The chat app's own routes do the rest -- start the
flow, replay the callback the desktop app would relay, and report the sign-in -- over a real PTY.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

from imbue.chat.accounts import read_index
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.pty_auth import spawn_pty
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.server import create_application
from imbue.chat.testing import build_test_state
from imbue.chat.testing import wait_until_true

_REPO_ROOT = Path(__file__).resolve().parents[5]

_FAKE_CLAUDE = """
import http.server, os, socket, subprocess, sys, urllib.parse

server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
port = server.server_address[1]
state = "fake-state"
redirect = urllib.parse.quote(f"http://localhost:{port}/callback", safe="")
url = f"https://claude.ai/oauth/authorize?code=true&client_id=c&redirect_uri={redirect}&state={state}"

class Callback(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        ok = query.get("state") == [state] and query.get("code") == ["the-code"]
        self.send_response(302 if ok else 400)
        self.send_header("Location", "https://platform.claude.com/oauth/code/success")
        self.send_header("Content-Length", "0")
        self.end_headers()
        server.succeeded = ok

    def log_message(self, *args):
        pass

server.RequestHandlerClass = Callback
server.succeeded = False
subprocess.run([os.environ["BROWSER"], url], check=True)
print("Browser didn't open? Use the url below to sign in:", flush=True)
print(url.replace("http%3A%2F%2Flocalhost", "https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode"), flush=True)
server.handle_request()
if server.succeeded:
    print("Login successful.", flush=True)
    sys.exit(0)
print("Login failed: the callback did not match", flush=True)
sys.exit(1)
"""


def _fake_claude_spawner(script: Path) -> Any:
    def spawner(_binary: str, _args: list[str], timeout: float, **kwargs: Any) -> Any:
        return spawn_pty(sys.executable, [str(script)], timeout, **kwargs)

    return spawner


def test_a_relayed_callback_signs_claude_in(tmp_path: Path) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE)
    service = AuthFlowService.create(
        home=None,
        work_dir=_REPO_ROOT,
        spawner=_fake_claude_spawner(script),
        probe=lambda *_a: SignedIn.NO,
    )
    client = create_application(build_test_state(auth_flows=service)).test_client()

    started = client.post("/api/accounts", json={"lane_id": "anthropic", "method_id": "subscription"}).get_json()
    relay_url = started["relay_url"]
    assert relay_url is not None and "redirect_uri=http%3A%2F%2Flocalhost%3A" in relay_url

    answer = client.post(
        f"/api/accounts/flow/{started['flow_id']}/callback",
        json={"path_and_query": "/callback?code=the-code&state=fake-state"},
    )

    assert answer.status_code == 200
    relayed = answer.get_json()
    assert relayed["status"] == 302
    assert relayed["location"] == "https://platform.claude.com/oauth/code/success"
    assert base64.b64decode(relayed["body"]) == b""
    wait_until_true(
        lambda: client.get(f"/api/accounts/flow/{started['flow_id']}").get_json()["state"] != "pending",
        10.0,
        "the sign-in settling",
    )
    assert client.get(f"/api/accounts/flow/{started['flow_id']}").get_json()["state"] == "ok"
    (account,) = read_index().accounts
    assert account.lane == "anthropic"


def test_a_callback_with_another_flows_state_never_reaches_the_cli(tmp_path: Path) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE)
    service = AuthFlowService.create(
        home=None,
        work_dir=_REPO_ROOT,
        spawner=_fake_claude_spawner(script),
        probe=lambda *_a: SignedIn.NO,
    )
    client = create_application(build_test_state(auth_flows=service)).test_client()
    started = client.post("/api/accounts", json={"lane_id": "anthropic", "method_id": "subscription"}).get_json()

    answer = client.post(
        f"/api/accounts/flow/{started['flow_id']}/callback",
        json={"path_and_query": "/callback?code=the-code&state=forged"},
    )

    assert answer.status_code == 409
    assert client.get(f"/api/accounts/flow/{started['flow_id']}").get_json()["state"] == "pending"
    client.delete(f"/api/accounts/flow/{started['flow_id']}")
