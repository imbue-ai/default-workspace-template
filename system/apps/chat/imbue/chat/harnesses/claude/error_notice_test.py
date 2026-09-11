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
    ("claude_kind", "kind", "text"),
    [
        pytest.param(
            "invalid_request",
            "invalid_request",
            "API Error: an image in the conversation could not be processed and was removed. Re-read the file"
            " with a different approach if you still need it.",
            id="invalid-request",
        ),
        pytest.param(
            "rate_limit",
            "rate_limit",
            "You've hit your session limit · resets 2:50pm (America/Los_Angeles)",
            id="rate-limit",
        ),
        pytest.param("overloaded", "overloaded", "API Error: Overloaded. Please try again later.", id="overloaded"),
        pytest.param(
            "model_not_found",
            "not_found",
            "There's an issue with the selected model. It may not exist or you may not have access to it.",
            id="model-not-found",
        ),
    ],
)
def test_a_failure_with_no_status_is_named_by_claude_codes_own_kind(claude_kind: str, kind: str, text: str) -> None:
    """Not every failure carries a status -- the image-attachment rejection above is a
    verbatim record that carries only ``error``. Each of these texts is invisible to BOTH
    fallbacks (the auth vocabulary and the prose classifier), so the kind can only come
    from Claude Code's own name for it, which is what pins the mapping."""
    notice = classify_error_notice(_stamped(error=claude_kind), text)
    assert notice.is_api_error is True
    assert notice.api_error_kind == kind


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


@pytest.mark.parametrize(
    ("record", "text"),
    [
        pytest.param(
            _stamped(error="invalid_request"),
            "Invalid API key · Fix external API key",
            id="prose-rescues-an-invalid-request-stamp",
        ),
        pytest.param(
            _stamped(error="invalid_request"),
            "Authentication error · The gateway could not authenticate with its upstream provider"
            " — contact your gateway administrator",
            id="prose-rescues-the-gateway-rejection",
        ),
        pytest.param(
            _stamped(error="authentication_failed", apiErrorStatus=403),
            "OAuth token revoked · Please run /login",
            id="stamp-outranks-the-status",
        ),
    ],
)
def test_neither_half_of_the_auth_precedence_can_be_dropped(record: dict[str, Any], text: str) -> None:
    """The auth question is settled by the stamp OR the prose, in that order, and each arm
    carries records the other misses.

    The first two are credential dead ends Claude Code files under `invalid_request`, a bucket
    that is mostly not credentials at all (request-too-large, tool-use concurrency) -- so the
    stamp cannot veto the prose without stranding them on the API-error surface with no way
    forward. The third is the converse: its wording is invisible to the auth vocabulary, and it
    carries a 403, which `kind_for_status` names `permission` -- so consulting the status before
    the stamp would relabel a revoked token as an ordinary failure and drop the sign-in button.
    """
    notice = classify_error_notice(record, text)
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


def test_an_error_field_that_is_not_a_name_is_ignored_rather_than_raised_on() -> None:
    """``error`` is read as a lookup key, so a record carrying a structured body there
    instead of a name would raise on an unhashable value -- inside the watcher thread,
    which wedges the read path for the rest of the session (the failure the null-message
    guard in the session parser prevents). The unreadable field is dropped, not the
    record: the status still names the failure."""
    notice = classify_error_notice(
        _stamped(error={"type": "rate_limit"}, apiErrorStatus=429),
        "You've hit your monthly spend limit",
    )
    assert notice.is_api_error is True
    assert notice.api_error_kind == "rate_limit"
