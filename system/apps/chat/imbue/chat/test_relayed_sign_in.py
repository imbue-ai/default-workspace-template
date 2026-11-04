"""A relayed Claude sign-in from end to end, against a stand-in for the `claude` CLI.

The stand-in does what `claude auth login` does on the wire: it listens on a loopback port, runs
`$BROWSER` with an authorize URL whose `redirect_uri` is that port, and prints "Login successful"
once the callback carrying its `state` arrives. The chat app's own routes do the rest -- start the
flow, deliver the callback the desktop app would relay, and answer with how the sign-in ended --
over a real PTY.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from flask.testing import FlaskClient

from imbue.chat.accounts import read_index
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.pty_auth import spawn_pty
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.server import create_application
from imbue.chat.testing import build_test_state

_REPO_ROOT = Path(__file__).resolve().parents[5]

_FAKE_CLAUDE_SCRIPT = Path(__file__).with_name("_fake_claude_login_script.py")


def _fake_claude_spawner(script: Path) -> Any:
    def spawner(_binary: str, _args: list[str], timeout: float, **kwargs: Any) -> Any:
        return spawn_pty(sys.executable, [str(script)], timeout, **kwargs)

    return spawner


def _start_claude_sign_in() -> tuple[FlaskClient, dict[str, Any]]:
    """A chat app whose `claude` is the stand-in, and the Claude sign-in it has started."""
    service = AuthFlowService.create(
        home=None,
        work_dir=_REPO_ROOT,
        spawner=_fake_claude_spawner(_FAKE_CLAUDE_SCRIPT),
        probe=lambda *_a: SignedIn.NO,
    )
    client = create_application(build_test_state(auth_flows=service)).test_client()
    started = client.post("/api/accounts", json={"lane_id": "anthropic", "method_id": "subscription"}).get_json()
    return client, started


def test_a_relayed_callback_signs_claude_in() -> None:
    client, started = _start_claude_sign_in()
    relay_url = started["relay_url"]
    assert relay_url is not None and "redirect_uri=http%3A%2F%2Flocalhost%3A" in relay_url

    answer = client.post(
        f"/api/accounts/flow/{started['flow_id']}/callback",
        json={"path_and_query": "/callback?code=the-code&state=fake-state"},
    )

    # The route waits for the CLI to finish, so its answer is the sign-in's outcome.
    assert answer.status_code == 200
    relayed = answer.get_json()
    assert (relayed["state"], relayed["provider_name"]) == ("ok", "Anthropic")
    (account,) = read_index().accounts
    assert relayed["account_id"] == account.id
    assert account.lane == "anthropic"


def test_a_callback_with_another_flows_state_never_reaches_the_cli() -> None:
    client, started = _start_claude_sign_in()

    answer = client.post(
        f"/api/accounts/flow/{started['flow_id']}/callback",
        json={"path_and_query": "/callback?code=the-code&state=forged"},
    )

    assert answer.status_code == 409
    assert client.get(f"/api/accounts/flow/{started['flow_id']}").get_json()["state"] == "pending"
    client.delete(f"/api/accounts/flow/{started['flow_id']}")
