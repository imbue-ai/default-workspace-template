"""Unit tests for reading Claude Code's own error stamp off a transcript record."""

from typing import Any

import pytest

from imbue.chat.harnesses.claude.error_notice import classify_error_notice
from imbue.chat.harnesses.error_patterns import is_provider_fault


def _stamped(**fields: Any) -> dict[str, Any]:
    """A record Claude Code marked as wrapping a failed request."""
    return {"type": "assistant", "isApiErrorMessage": True, **fields}


def test_a_rate_limit_whose_prose_names_no_status_is_still_an_error() -> None:
    """The failure that motivated reading the stamp: Claude Code's limit notices carry the
    status as a field and nothing resembling one in their wording, so matching the prose
    left the whole rate-limit family rendering as ordinary assistant output."""
    notice = classify_error_notice(
        _stamped(apiErrorStatus=429, error="rate_limit"),
        "You've hit your monthly spend limit · raise it at claude.ai/settings/usage?from=cc_cli_limit_message",
    )
    assert notice.is_api_error is True
    assert notice.api_error_kind == "rate_limit"
    assert is_provider_fault(notice.api_error_kind) is False
    assert notice.is_auth_error is False


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        pytest.param(529, "overloaded", id="overloaded"),
        pytest.param(500, "api_error", id="internal-server-error"),
    ],
)
def test_a_5xx_is_the_providers_fault(status: int, kind: str) -> None:
    notice = classify_error_notice(_stamped(apiErrorStatus=status, error="server_error"), "API Error: whatever")
    assert notice.api_error_kind == kind
    assert is_provider_fault(notice.api_error_kind) is True


def test_a_server_error_with_no_status_is_not_blamed_on_the_provider() -> None:
    """Claude Code stamps `server_error` on failures that never reached a server -- a
    laptop that slept mid-response, a DNS failure. Those are still errors, but calling
    them the provider's fault would print a confidently wrong cause over a local one."""
    notice = classify_error_notice(
        _stamped(error="server_error"),
        "API Error: Your computer went to sleep mid-response. The response above may be incomplete.",
    )
    assert notice.is_api_error is True
    assert notice.api_error_kind is None
    assert is_provider_fault(notice.api_error_kind) is False


def test_a_404_names_the_model_rather_than_the_provider() -> None:
    notice = classify_error_notice(
        _stamped(apiErrorStatus=404, error="model_not_found"),
        "There's an issue with the selected model (claude-fable-5).",
    )
    assert notice.api_error_kind == "not_found"
    assert is_provider_fault(notice.api_error_kind) is False


@pytest.mark.parametrize(
    ("claude_kind", "text"),
    [
        pytest.param(
            "authentication_failed",
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            id="oauth-session-expired",
        ),
        pytest.param("authentication_failed", "Login expired · Please run /login", id="login-expired"),
        pytest.param("billing_error", "Credit balance is too low", id="credit-balance"),
        pytest.param("account_on_hold", "Your account is on hold.", id="account-on-hold"),
        pytest.param("oauth_org_not_allowed", "Your organization does not allow this.", id="org-disallows-oauth"),
    ],
)
def test_the_credential_family_keeps_its_own_surface(claude_kind: str, text: str) -> None:
    """These end the turn the same way an expired token does -- the only way forward is
    different credentials -- so they route to the sign-in surface, and never carry the
    API-error subtext as well. Two of them say so in wording the auth vocabulary does not
    match, which is exactly what the stamp settles."""
    notice = classify_error_notice(_stamped(error=claude_kind), text)
    assert notice.is_auth_error is True
    assert notice.is_api_error is False
    assert notice.api_error_kind is None


def test_an_unstamped_notice_falls_back_to_its_prose() -> None:
    """A record from a Claude Code build that predates the stamp still classifies."""
    notice = classify_error_notice({"type": "assistant"}, "API Error: 529 Overloaded")
    assert notice.is_api_error is True
    assert notice.api_error_kind == "overloaded"


def test_a_synthetic_message_that_is_not_a_failure_is_not_an_error() -> None:
    notice = classify_error_notice({"type": "assistant", "isApiErrorMessage": False}, "Continuing.")
    assert notice.is_api_error is False
    assert notice.is_auth_error is False
    assert notice.api_error_kind is None
