"""Unit tests for the shared credentials/quota failure patterns."""

from imbue.system_interface.harnesses.auth_patterns import is_auth_error_text

# Verbatim from a live pi agent: Anthropic rejects third-party usage once the extra-usage
# balance runs out. It arrives as a 400 invalid_request_error, so only the message text
# distinguishes it from an ordinary bad request.
_EXTRA_USAGE_EXHAUSTED = (
    '400 {"type":"error","error":{"type":"invalid_request_error","message":"Third-party apps now '
    "draw from your extra usage, not your plan limits. Add more at claude.ai/settings/usage and "
    'keep going."},"request_id":"req_011CeQNupztwPtoPGrLhoqpJ"}'
)


def test_exhausted_extra_usage_is_the_credentials_family() -> None:
    assert is_auth_error_text(_EXTRA_USAGE_EXHAUSTED) is True


def test_claude_surface_forms_still_match() -> None:
    assert is_auth_error_text("Not logged in · Please run /login") is True
    assert is_auth_error_text("API Error: 401 Unauthorized") is True
    assert is_auth_error_text('{"type": "authentication_error"}') is True


def test_bare_401_matches_only_at_the_start() -> None:
    # pi's form carries no "API Error:" prefix; a 401 mid-sentence is not a failure.
    assert is_auth_error_text('401 {"type":"error"}') is True
    assert is_auth_error_text("a 401 means your key expired") is False


def test_ordinary_text_is_not_an_auth_error() -> None:
    assert is_auth_error_text("Here's the fix.") is False
    assert is_auth_error_text("") is False
