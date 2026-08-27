"""Integration tests for the /api/claude-auth/* endpoints.

Each test builds a `ClaudeAuthService` and/or `WelcomeResender` with
deterministic fakes and passes them to `create_application`, which stores
them on the app's `SystemInterfaceState` for the handlers to read. This
exercises the auth-success chokepoint end-to-end through the Flask test
client without touching real Claude binaries or session transcripts -- and
without `unittest.mock` or runtime attribute patching.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from imbue.system_interface import welcome_resend
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_discovery import SendFailure
from imbue.system_interface.harnesses.claude.auth import ClaudeAuthService
from imbue.system_interface.harnesses.claude.auth import ProcessSetupError
from imbue.system_interface.server import create_application
from imbue.system_interface.testing import FakeFinishedProcess
from imbue.system_interface.accounts import account_dir
from imbue.system_interface.accounts import read_index
from imbue.system_interface.harnesses.auth_flows import AuthFlowService
from imbue.system_interface.testing import FakePexpectProcess
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.welcome_resend import WelcomeResender

# The initial chat agent's id, as the bootstrap would persist it.
_CHAT_AGENT_ID = "agent-00000000000000000000000000000001"

_FAKE_URL = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
_FAKE_TOKEN = "sk-ant-oat01-" + "ENDPOINTFAKE" * 8 + "x"

_LIST_PAYLOAD = json.dumps(
    {
        "agents": [
            {"name": "ababa", "type": "claude", "state": "RUNNING"},
            {"name": "system-services", "type": "main", "state": "RUNNING"},
            {"name": "worker-1", "type": "claude", "state": "WAITING"},
        ]
    }
)


@pytest.fixture
def isolated_claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    return config_dir


def _fake_chat_agent() -> AgentInfo:
    """A resolved initial-chat-agent AgentInfo (valid id) for welcome-resend tests."""
    return AgentInfo(
        id=_CHAT_AGENT_ID,
        name="chat",
        state="RUNNING",
        agent_state_dir=Path("/tmp/agent"),
        claude_config_dir=Path("/tmp/.claude"),
    )


def _persist_chat_agent_id(host_dir: Path) -> None:
    """Write the initial chat agent's id where welcome_resend reads it back."""
    (host_dir / welcome_resend._INITIAL_CHAT_AGENT_ID_FILENAME).write_text(_CHAT_AGENT_ID)


@contextmanager
def _client(
    claude_auth_service: ClaudeAuthService | None = None,
    welcome_resender: WelcomeResender | None = None,
    auth_flows: AuthFlowService | None = None,
) -> Iterator[FlaskClient]:
    """Build a Flask test client, injecting the auth collaborators into the app state.

    Each argument left as None gets a default production instance -- fine for
    tests that never reach that dependency (e.g. request-validation rejections).
    """
    state = build_test_state(
        claude_auth_service=claude_auth_service, welcome_resender=welcome_resender, auth_flows=auth_flows
    )
    yield create_application(state).test_client()


def _logged_in_runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
    return FakeFinishedProcess(stdout='{"loggedIn": true, "email": "u@example.com", "subscriptionType": "Max"}')


def _build_welcome_resender(host_dir: Path, welcome_calls: list[str]) -> WelcomeResender:
    _persist_chat_agent_id(host_dir)
    skill_path = host_dir / "SKILL.md"
    skill_path.write_text("---\nname: w\n---\n\nIntro\n\n---\n\n### Welcome to Minds\n\nbody\n\n---\n")

    def _record_welcome_send(agent_id: str, _message: str) -> SendFailure | None:
        welcome_calls.append(agent_id)
        return None

    return WelcomeResender(
        resolve_agent=lambda _id: _fake_chat_agent(),
        read_assistant_transcript=lambda _agent: "",
        send_message_fn=_record_welcome_send,
        skill_path=skill_path,
    )


def test_status_endpoint_returns_parsed_payload(isolated_claude_config: Path) -> None:
    service = ClaudeAuthService(command_runner=_logged_in_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["logged_in"] is True
    assert payload["email"] == "u@example.com"
    assert payload["subscription_type"] == "Max"
    assert payload["auth_mode"] == "none"


def test_status_endpoint_reports_settings_derived_mode(isolated_claude_config: Path) -> None:
    (isolated_claude_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-1234", "ANTHROPIC_BASE_URL": "https://litellm.example"}})
    )
    service = ClaudeAuthService(command_runner=_logged_in_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    payload = response.get_json()
    assert payload["auth_mode"] == "imbue"
    assert payload["masked_key_suffix"] == "1234"


def test_status_endpoint_logged_out_when_claude_missing(isolated_claude_config: Path) -> None:
    def _missing_runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        raise ProcessSetupError(command=("claude",), stdout="", stderr="not found", is_output_already_logged=False)

    service = ClaudeAuthService(command_runner=_missing_runner)
    with _client(claude_auth_service=service) as client:
        response = client.get("/api/claude-auth/status")
    assert response.status_code == 200
    assert response.get_json()["logged_in"] is False







def test_submit_credentials_rejects_unmanaged_keys(isolated_claude_config: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": "SOME_RANDOM_KEY=x"},
        )
    assert response.status_code == 400
    assert "Unsupported keys" in response.get_json()["detail"]


def test_submit_credentials_rejects_mixed_modes(isolated_claude_config: Path) -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": f"ANTHROPIC_API_KEY=sk-1\nCLAUDE_CODE_OAUTH_TOKEN={_FAKE_TOKEN}"},
        )
    assert response.status_code == 400
    assert "not both" in response.get_json()["detail"]


def test_submit_credentials_rejects_empty_body() -> None:
    with _client() as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": "   "},
        )
    assert response.status_code == 400





def test_submit_credentials_mints_an_account_rather_than_overwriting_the_shared_login(
    tmp_path: Path,
) -> None:
    """The Imbue path: a credential someone else obtained becomes an account of its own.

    It used to overwrite the workspace's shared settings.json and restart every claude
    agent to make them see it. Now the paste lands in a fresh account folder, so nothing
    running is disturbed and the account's existence is the signed-in-with-Imbue flag.
    """
    flows = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work")

    with _client(auth_flows=flows) as client:
        response = client.post(
            "/api/claude-auth/submit-credentials",
            json={"credentials": "ANTHROPIC_API_KEY=sk-ant-test-key\nANTHROPIC_BASE_URL=https://proxy"},
        )

    assert response.status_code == 200
    account_id = response.get_json()["account_id"]
    settings = json.loads((account_dir(account_id, tmp_path) / "settings.json").read_text())
    # Both keys: a base URL is the whole point of the proxied setup, and dropping it would
    # silently route to Anthropic instead.
    assert settings["env"] == {"ANTHROPIC_API_KEY": "sk-ant-test-key", "ANTHROPIC_BASE_URL": "https://proxy"}
    assert [a.id for a in read_index(tmp_path).accounts] == [account_id]
