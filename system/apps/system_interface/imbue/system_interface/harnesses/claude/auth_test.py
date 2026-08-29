"""Tests for the claude_auth backend module.

`ClaudeAuthService` takes its outside-world dependencies (`command_runner`,
`pexpect_spawner`) as constructor arguments, so each test builds an
isolated instance with deterministic fakes -- no `unittest.mock`, and no
runtime patching of module attributes. The pure module-level helpers
(`_parse_status_payload`, `parse_credential_lines`, `derive_auth_mode`,
the settings-env reader/writer, the URL/token extraction) are tested
directly. `CLAUDE_CONFIG_DIR` is pointed at a tmp dir via
`monkeypatch.setenv` (environment adjustment, not object patching) so no
test reads the developer's real shared Claude settings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from imbue.system_interface.harnesses.claude import auth
from imbue.system_interface.testing import FakeFinishedProcess

_FAKE_URL = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
_FAKE_TOKEN = "sk-ant-oat01-" + "FAKETOKEN0" * 9 + "12345"


@pytest.fixture
def isolated_claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point CLAUDE_CONFIG_DIR at a tmp dir so tests never touch real settings."""
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    return config_dir


# ----- status parsing -----


def test_parse_status_payload_full() -> None:
    payload: dict[str, object] = {
        "loggedIn": True,
        "authMethod": "oauth",
        "apiProvider": "claudeai",
        "email": "user@example.com",
        "orgId": "org-1",
        "orgName": "Example",
        "subscriptionType": "Max",
    }
    status = auth._parse_status_payload(payload)
    assert status.logged_in is True
    assert status.email == "user@example.com"
    assert status.subscription_type == "Max"


def test_parse_status_payload_minimal() -> None:
    status = auth._parse_status_payload({"loggedIn": False})
    assert status.logged_in is False
    assert status.email is None
    assert status.subscription_type is None


def test_parse_status_payload_empty_strings_coerced_to_none() -> None:
    payload: dict[str, object] = {"loggedIn": True, "email": "", "subscriptionType": ""}
    status = auth._parse_status_payload(payload)
    assert status.email is None
    assert status.subscription_type is None


def test_get_auth_status_returns_logged_out_when_runner_raises(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        raise auth.ProcessSetupError(
            command=("claude",), stdout="", stderr="not found", is_output_already_logged=False
        )

    service = auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is False
    assert status.auth_mode is auth.AuthMode.NONE


def test_get_auth_status_parses_logged_in_json(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout='{"loggedIn": true, "email": "x@y.com", "subscriptionType": "Pro"}')

    service = auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is True
    assert status.email == "x@y.com"
    assert status.subscription_type == "Pro"


def test_get_auth_status_rejects_non_json_output(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout="not json at all")

    service = auth.ClaudeAuthService(command_runner=_runner)
    with pytest.raises(auth.ClaudeAuthError, match="non-JSON"):
        service.get_auth_status()


def test_get_auth_status_treats_empty_output_as_logged_out(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout="")

    service = auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.logged_in is False


def test_get_auth_status_overlays_managed_settings_env_onto_subprocess(isolated_claude_config: Path) -> None:
    """The status subprocess must see the settings-managed credentials.

    The long-lived system-interface process never receives settings-env
    values in its own environment, so `get_auth_status` overlays whatever
    is currently in the managed env block -- otherwise a freshly written
    key would misreport as logged out.
    """
    (isolated_claude_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-managed-key"}})
    )
    seen_envs: list[dict[str, str] | None] = []

    def _runner(_cmd: list[str], _timeout: float, env: dict[str, str] | None = None) -> FakeFinishedProcess:
        seen_envs.append(env)
        return FakeFinishedProcess(stdout='{"loggedIn": true, "authMethod": "api_key"}')

    service = auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.auth_mode is auth.AuthMode.API_KEY
    assert status.masked_key_suffix == "-key"
    assert seen_envs and seen_envs[0] is not None
    assert seen_envs[0]["ANTHROPIC_API_KEY"] == "sk-ant-managed-key"


# ----- credential-lines parsing -----


def test_parse_credential_lines_accepts_api_key_alone() -> None:
    parsed = auth.parse_credential_lines("ANTHROPIC_API_KEY=sk-ant-abc")
    assert parsed == {"ANTHROPIC_API_KEY": "sk-ant-abc"}


def test_parse_credential_lines_accepts_key_with_base_url() -> None:
    parsed = auth.parse_credential_lines(
        "ANTHROPIC_BASE_URL=https://litellm.example.com\nANTHROPIC_API_KEY=sk-litellm-1"
    )
    assert parsed == {
        "ANTHROPIC_BASE_URL": "https://litellm.example.com",
        "ANTHROPIC_API_KEY": "sk-litellm-1",
    }


def test_parse_credential_lines_accepts_oauth_token_alone() -> None:
    parsed = auth.parse_credential_lines(f"CLAUDE_CODE_OAUTH_TOKEN={_FAKE_TOKEN}")
    assert parsed == {"CLAUDE_CODE_OAUTH_TOKEN": _FAKE_TOKEN}


def test_parse_credential_lines_rejects_unknown_keys() -> None:
    with pytest.raises(auth.CredentialPasteError, match="Unsupported keys.*SOME_OTHER_KEY"):
        auth.parse_credential_lines("ANTHROPIC_API_KEY=sk-1\nSOME_OTHER_KEY=x")


def test_parse_credential_lines_rejects_mixed_token_and_key() -> None:
    with pytest.raises(auth.CredentialPasteError, match="not both"):
        auth.parse_credential_lines(f"ANTHROPIC_API_KEY=sk-1\nCLAUDE_CODE_OAUTH_TOKEN={_FAKE_TOKEN}")


def test_parse_credential_lines_rejects_base_url_without_key() -> None:
    with pytest.raises(auth.CredentialPasteError, match="requires an accompanying"):
        auth.parse_credential_lines("ANTHROPIC_BASE_URL=https://litellm.example.com")


def test_parse_credential_lines_rejects_empty_paste() -> None:
    with pytest.raises(auth.CredentialPasteError, match="No credentials found"):
        auth.parse_credential_lines("   \n# just a comment\n")


def test_parse_credential_lines_strips_quotes_and_whitespace() -> None:
    parsed = auth.parse_credential_lines('ANTHROPIC_API_KEY="sk-ant-quoted"  ')
    assert parsed == {"ANTHROPIC_API_KEY": "sk-ant-quoted"}


# ----- mode derivation -----


def test_derive_auth_mode_covers_all_shapes() -> None:
    assert auth.derive_auth_mode({}) is auth.AuthMode.NONE
    assert auth.derive_auth_mode({"ANTHROPIC_API_KEY": "k"}) is auth.AuthMode.API_KEY
    assert auth.derive_auth_mode({"ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "u"}) is auth.AuthMode.IMBUE
    assert auth.derive_auth_mode({"CLAUDE_CODE_OAUTH_TOKEN": "t"}) is auth.AuthMode.SUBSCRIPTION


def test_derive_auth_mode_key_outranks_token() -> None:
    """Mirrors Claude Code's own precedence: a key present wins over a token."""
    managed = {"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"}
    assert auth.derive_auth_mode(managed) is auth.AuthMode.API_KEY


def test_masked_credential_suffix_prefers_key_and_handles_absence() -> None:
    assert auth.masked_credential_suffix({}) is None
    assert auth.masked_credential_suffix({"ANTHROPIC_API_KEY": "sk-ant-abcd"}) == "abcd"
    assert auth.masked_credential_suffix({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-wxyz"}) == "wxyz"


# ----- settings-env reader/writer -----












def test_read_managed_auth_env_returns_only_managed_keys(isolated_claude_config: Path) -> None:
    (isolated_claude_config / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-1", "OTHER": "x"}})
    )
    assert auth.read_managed_auth_env() == {"ANTHROPIC_API_KEY": "sk-1"}


def test_read_managed_auth_env_tolerates_missing_and_corrupt_files(isolated_claude_config: Path) -> None:
    assert auth.read_managed_auth_env() == {}
    (isolated_claude_config / "settings.json").write_text("{broken")
    assert auth.read_managed_auth_env() == {}


# ----- config dir / .claude.json resolution -----


def test_resolution_defaults_to_home_claude_when_config_dir_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With CLAUDE_CONFIG_DIR unset (the workspace-wide default since the
    ~/.claude cutover) the config dir is ~/.claude and the global config is
    ~/.claude.json -- BESIDE the dir, not inside it, matching where claude
    itself reads the API-key approval from."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert auth._resolve_claude_config_dir() == tmp_path / ".claude"
    assert auth._resolve_claude_json_path() == tmp_path / ".claude.json"


def test_resolution_honors_explicit_config_dir_env(isolated_claude_config: Path) -> None:
    """An explicitly exported CLAUDE_CONFIG_DIR pins both the config dir and
    the .claude.json inside it (claude's set-var layout)."""
    assert auth._resolve_claude_config_dir() == isolated_claude_config
    assert auth._resolve_claude_json_path() == isolated_claude_config / ".claude.json"


# ----- workspace id -----


def test_read_workspace_id_prefers_the_services_agents_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"host_id": "host-123"}))
    services_dir = tmp_path / "agents" / "agent-abc"
    services_dir.mkdir(parents=True)
    (services_dir / "data.json").write_text(
        json.dumps({"id": "agent-abc", "name": "system-services", "labels": {"is_primary": "true"}})
    )
    chat_dir = tmp_path / "agents" / "agent-chat"
    chat_dir.mkdir(parents=True)
    (chat_dir / "data.json").write_text(json.dumps({"id": "agent-chat", "labels": {}}))
    assert auth.read_workspace_id() == "agent-abc"


def test_read_workspace_id_falls_back_to_the_machines_host_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    (tmp_path / "data.json").write_text(json.dumps({"host_id": "host-123"}))
    assert auth.read_workspace_id() == "host-123"


def test_read_workspace_id_tolerates_missing_env_and_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_HOST_DIR", raising=False)
    assert auth.read_workspace_id() is None
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    assert auth.read_workspace_id() is None


# ----- agent snapshot / restart -----

_LIST_PAYLOAD = json.dumps(
    {
        "agents": [
            {"name": "chat-1", "type": "claude", "state": "RUNNING"},
            {"name": "system-services", "type": "main", "state": "RUNNING"},
            {"name": "worker-1", "type": "claude", "state": "RUNNING"},
            {"name": "extra-chat", "type": "claude", "state": "WAITING"},
            {"name": "old-chat", "type": "claude", "state": "STOPPED"},
        ]
    }
)





def _build_restart_recording_service(
    command_log: list[tuple[str, ...]],
) -> auth.ClaudeAuthService:
    def _runner(cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        command_log.append(tuple(cmd))
        if cmd[1] == "list":
            return FakeFinishedProcess(stdout=_LIST_PAYLOAD)
        return FakeFinishedProcess(returncode=0, stdout='{"loggedIn": true}')

    return auth.ClaudeAuthService(command_runner=_runner)










# ----- submit_credentials -----










# ----- record_api_key_approval -----










# ----- setup-token flow -----
























# ----- browser sign-in (claude auth login) flow -----
# Oauth pump pattern order: success=0, failed=1, OAuth-error=2, EOF=3, TIMEOUT=4.


















# ----- credentials-based mode folding -----


def test_status_folds_subscription_mode_from_credentials_when_env_empty(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(
            stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max", "email": "x@y.com"}'
        )

    service = auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.auth_mode is auth.AuthMode.SUBSCRIPTION


def test_status_folds_console_mode_when_claude_ai_without_subscription(isolated_claude_config: Path) -> None:
    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout='{"loggedIn": true, "authMethod": "claude.ai"}')

    service = auth.ClaudeAuthService(command_runner=_runner)
    status = service.get_auth_status()
    assert status.auth_mode is auth.AuthMode.CONSOLE


def test_status_managed_env_outranks_credentials_fold(isolated_claude_config: Path) -> None:
    (isolated_claude_config / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-key"}}))

    def _runner(_cmd: list[str], _timeout: float, _env: object = None) -> FakeFinishedProcess:
        return FakeFinishedProcess(stdout='{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "Max"}')

    service = auth.ClaudeAuthService(command_runner=_runner)
    assert service.get_auth_status().auth_mode is auth.AuthMode.API_KEY


# ----- token/URL extraction -----














def test_extract_oauth_url_prefers_osc8_hyperlink_target_over_garbled_label() -> None:
    """The CLI's visible wrapped label render is garbled; the OSC 8 target
    (id-parameterized, BEL-terminated) carries the intact URL."""
    full_url = _FAKE_URL + "&redirect_uri=https%3A%2F%2Fx&state=S123"
    raw = f"\x1b]8;id=1abc;{full_url}\x07\x1b[38;5;246m{full_url[:80]}\x1b[39m\x1b]8;;\x07"
    assert auth._extract_oauth_url(raw) == full_url


def test_extract_oauth_url_returns_none_when_no_url_present() -> None:
    assert auth._extract_oauth_url("no links here") is None


# ----- repo<->mngr CLI contract -----
# These assert the argv shapes we hand to subprocesses are accepted by the
# LIVE mngr CLI (parse-only), so a CLI flag rename breaks these tests
# instead of runtime behavior in a deployed mind.






