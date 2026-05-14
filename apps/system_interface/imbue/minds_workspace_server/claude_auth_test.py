"""Tests for the claude_auth backend module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from imbue.minds_workspace_server import claude_auth


def test_parse_status_payload_full() -> None:
    payload = {
        "loggedIn": True,
        "authMethod": "oauth",
        "apiProvider": "claudeai",
        "email": "user@example.com",
        "orgId": "org-1",
        "orgName": "Example",
        "subscriptionType": "Max",
    }
    status = claude_auth._parse_status_payload(payload)
    assert status.logged_in is True
    assert status.email == "user@example.com"
    assert status.subscription_type == "Max"


def test_parse_status_payload_minimal() -> None:
    status = claude_auth._parse_status_payload({"loggedIn": False})
    assert status.logged_in is False
    assert status.email is None
    assert status.subscription_type is None


def test_parse_status_payload_empty_strings_coerced_to_none() -> None:
    status = claude_auth._parse_status_payload(
        {"loggedIn": True, "email": "", "subscriptionType": ""}
    )
    assert status.email is None
    assert status.subscription_type is None


def test_get_auth_status_handles_missing_claude_binary() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        status = claude_auth.get_auth_status()
    assert status.logged_in is False


def test_get_auth_status_handles_timeout() -> None:
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=10)):
        status = claude_auth.get_auth_status()
    assert status.logged_in is False


def test_get_auth_status_parses_logged_in_json() -> None:
    fake_result = MagicMock()
    fake_result.stdout = '{"loggedIn": true, "email": "x@y.com", "subscriptionType": "Pro"}'
    with patch("subprocess.run", return_value=fake_result):
        status = claude_auth.get_auth_status()
    assert status.logged_in is True
    assert status.email == "x@y.com"
    assert status.subscription_type == "Pro"


def test_get_auth_status_rejects_non_json_output() -> None:
    fake_result = MagicMock()
    fake_result.stdout = "not json at all"
    with patch("subprocess.run", return_value=fake_result):
        with pytest.raises(claude_auth.ClaudeAuthError, match="non-JSON"):
            claude_auth.get_auth_status()


def test_get_auth_status_treats_empty_output_as_logged_out() -> None:
    fake_result = MagicMock()
    fake_result.stdout = ""
    with patch("subprocess.run", return_value=fake_result):
        status = claude_auth.get_auth_status()
    assert status.logged_in is False


def test_format_env_file_simple() -> None:
    text = claude_auth._format_env_file({"FOO": "bar"})
    assert text == "FOO=bar\n"


def test_format_env_file_quotes_values_with_spaces() -> None:
    text = claude_auth._format_env_file({"FOO": "bar baz"})
    assert text == 'FOO="bar baz"\n'


def test_write_api_key_creates_file_when_missing(tmp_path: Path) -> None:
    env_path = tmp_path / "env"
    claude_auth.write_api_key_to_host_env(SecretStr("sk-ant-test"), env_path_override=env_path)
    assert env_path.read_text() == "ANTHROPIC_API_KEY=sk-ant-test\n"


def test_write_api_key_updates_existing_file(tmp_path: Path) -> None:
    env_path = tmp_path / "env"
    env_path.write_text("CLAUDE_CONFIG_DIR=/some/path\nANTHROPIC_API_KEY=old\n")
    claude_auth.write_api_key_to_host_env(SecretStr("sk-ant-new"), env_path_override=env_path)
    text = env_path.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-new" in text
    assert "CLAUDE_CONFIG_DIR=/some/path" in text
    assert "old" not in text


def test_submit_oauth_code_rejects_unknown_session() -> None:
    claude_auth.abort_oauth_login()
    with pytest.raises(claude_auth.ClaudeAuthError, match="No active OAuth session"):
        claude_auth.submit_oauth_code("bogus", "fake#code")


def test_oauth_session_extracts_url_from_pty_stdout() -> None:
    """Mock pexpect.spawn so we can exercise the URL-parse path without invoking claude."""
    fake_url = "https://claude.ai/oauth/authorize?code=abc&state=def"
    fake_process = MagicMock()
    fake_process.expect = MagicMock(return_value=0)
    fake_process.match = MagicMock()
    fake_process.match.group = MagicMock(return_value=fake_url)
    fake_process.isalive = MagicMock(return_value=True)
    fake_process.sendline = MagicMock()
    fake_process.terminate = MagicMock()
    with patch("pexpect.spawn", return_value=fake_process):
        claude_auth.abort_oauth_login()
        result = claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
    assert result.oauth_url == fake_url
    assert result.session_id
    claude_auth.abort_oauth_login()


def test_oauth_session_raises_on_eof_before_url() -> None:
    fake_process = MagicMock()
    fake_process.expect = MagicMock(return_value=1)
    fake_process.isalive = MagicMock(return_value=True)
    fake_process.terminate = MagicMock()
    with patch("pexpect.spawn", return_value=fake_process):
        claude_auth.abort_oauth_login()
        with pytest.raises(claude_auth.ClaudeAuthError, match="before printing OAuth URL"):
            claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)


def test_oauth_session_raises_on_timeout_waiting_for_url() -> None:
    fake_process = MagicMock()
    fake_process.expect = MagicMock(return_value=2)
    fake_process.isalive = MagicMock(return_value=True)
    fake_process.terminate = MagicMock()
    with patch("pexpect.spawn", return_value=fake_process):
        claude_auth.abort_oauth_login()
        with pytest.raises(claude_auth.ClaudeAuthError, match="Timed out"):
            claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)


def test_submit_oauth_code_drives_subprocess_and_returns_status() -> None:
    fake_url = "https://claude.ai/oauth/authorize?x=1"
    fake_process = MagicMock()
    fake_process.expect = MagicMock(return_value=0)
    fake_process.match = MagicMock()
    fake_process.match.group = MagicMock(return_value=fake_url)
    fake_process.isalive = MagicMock(return_value=True)
    fake_process.sendline = MagicMock()
    fake_process.terminate = MagicMock()
    fake_status_result = MagicMock()
    fake_status_result.stdout = '{"loggedIn": true, "email": "x@y.com"}'
    with patch("pexpect.spawn", return_value=fake_process):
        claude_auth.abort_oauth_login()
        start = claude_auth.start_oauth_login(claude_auth.OAuthProvider.CLAUDEAI)
        with patch("subprocess.run", return_value=fake_status_result):
            status = claude_auth.submit_oauth_code(start.session_id, "CODE#STATE")
    assert status.logged_in is True
    assert status.email == "x@y.com"
    fake_process.sendline.assert_called_once_with("CODE#STATE")
