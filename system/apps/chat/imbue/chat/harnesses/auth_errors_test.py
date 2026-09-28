"""Tests for the shared auth-error vocabulary.

Every string below was captured from the pinned CLIs by driving them against a deliberately
bogus credential -- not written from documentation. The negatives matter as much: a rate
limit, a network failure and a model refusal all end a turn, and flagging one of those sends
the user to sign in again over something signing in cannot fix.
"""

from __future__ import annotations

import pytest

from imbue.chat.harnesses.auth_errors import is_auth_error_text

_CODEX_401 = (
    "unexpected status 401 Unauthorized: Incorrect API key provided: sk-bogus000. "
    "You can find your API key at https://platform.openai.com/account/api-keys., "
    "url: https://api.openai.com/v1/responses, cf-ray: a31d, request id: req_e96, "
    "auth error: 401, auth error code: invalid_api_key"
)
_PI_401 = (
    '401 {"type":"error","error":{"type":"authentication_error","message":"API key is invalid."},"request_id":null}'
)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_CODEX_401, id="codex-bogus-key"),
        pytest.param(_PI_401, id="pi-bogus-key"),
        pytest.param("Please sign in to view available models.", id="agy-signed-out"),
        pytest.param("Error: authentication failed or timed out", id="agy-auth-failed"),
        pytest.param("API Error: 401 Unauthorized", id="claude-style-401"),
        pytest.param("Not logged in", id="prose"),
    ],
)
def test_a_credential_failure_is_recognised(text: str) -> None:
    assert is_auth_error_text(text) is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("rate limit exceeded, please try again in 30s", id="rate-limit"),
        pytest.param("Connection reset by peer", id="network"),
        pytest.param("500 Internal Server Error", id="server-fault"),
        pytest.param("I cannot help with that request.", id="model-refusal"),
        pytest.param("The tool call failed: file not found", id="tool-failure"),
    ],
)
def test_an_error_signing_in_cannot_fix_is_not_flagged(text: str) -> None:
    """The notice offers a re-auth. Offering it for a rate limit teaches the user to ignore
    the notice, which is worse than not showing one."""
    assert is_auth_error_text(text) is False


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("This organization has been disabled", id="claude-org-disabled"),
        pytest.param("OAuth token does not meet scope requirements", id="claude-oauth-scope"),
        pytest.param("Authentication Error, Invalid proxy server token passed", id="litellm-proxy-token"),
    ],
)
def test_folded_claude_patterns(text: str) -> None:
    """These came from `claude/auth_patterns.py`, now folded in."""
    assert is_auth_error_text(text) is True


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Credit balance is too low", id="claude-credit-balance"),
        pytest.param("Budget has been exceeded for this key", id="litellm-budget"),
        pytest.param("usage_limit_exceeded", id="codex-quota"),
        pytest.param('400 {"type":"invalid_request_error","message":"prompt is too long"}', id="bad-request"),
    ],
)
def test_a_spent_account_or_a_bad_request_is_not_a_sign_in_problem(text: str) -> None:
    """The credential works in each of these; signing in again would change nothing."""
    assert is_auth_error_text(text) is False
