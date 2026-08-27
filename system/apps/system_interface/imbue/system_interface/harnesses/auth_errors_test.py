"""Tests for the shared auth-error vocabulary.

Every string below was captured from the pinned CLIs by driving them against a deliberately
bogus credential -- not written from documentation. The negatives matter as much: a rate
limit, a network failure and a model refusal all end a turn, and flagging one of those sends
the user to sign in again over something signing in cannot fix.
"""

from __future__ import annotations

import pytest

from imbue.system_interface.harnesses.auth_errors import is_auth_error_text

_CODEX_401 = (
    "unexpected status 401 Unauthorized: Incorrect API key provided: sk-bogus000. "
    "You can find your API key at https://platform.openai.com/account/api-keys., "
    "url: https://api.openai.com/v1/responses, cf-ray: a31d, request id: req_e96, "
    "auth error: 401, auth error code: invalid_api_key"
)
_PI_401 = '401 {"type":"error","error":{"type":"authentication_error","message":"API key is invalid."},"request_id":null}'


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
