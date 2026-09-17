"""Unit tests for the Claude API-error classifier."""

import pytest

from imbue.chat.harnesses.error_patterns import classify_api_error
from imbue.chat.harnesses.error_patterns import is_provider_fault
from imbue.chat.harnesses.error_patterns import kind_for_status


def test_overloaded_is_a_provider_fault() -> None:
    kind = classify_api_error("API Error: 529 Overloaded")
    assert kind == "overloaded"
    assert is_provider_fault(kind) is True


def test_internal_server_error_is_a_provider_fault() -> None:
    kind = classify_api_error("API Error: 500 Internal server error")
    assert kind == "api_error"
    assert is_provider_fault(kind) is True


def test_service_unavailable_is_a_provider_fault() -> None:
    assert classify_api_error("API Error: 503 Service Unavailable") == "overloaded"


def test_rate_limit_is_an_error_but_not_a_provider_fault() -> None:
    kind = classify_api_error("API Error: 429 rate_limit_error")
    assert kind == "rate_limit"
    assert is_provider_fault(kind) is False


def test_invalid_request_is_a_client_error() -> None:
    kind = classify_api_error("API Error: 400 Bad Request")
    assert kind == "invalid_request"
    assert is_provider_fault(kind) is False


def test_embedded_error_type_json_is_recognized() -> None:
    kind = classify_api_error('{"type": "overloaded_error", "message": "Overloaded"}')
    assert kind == "overloaded"
    assert is_provider_fault(kind) is True


def test_auth_errors_are_not_reclassified_here() -> None:
    # 401 / authentication_error are owned by auth_patterns.py (they have their own
    # recovery surface), so the API-error classifier leaves them alone.
    assert classify_api_error("API Error: 401 Unauthorized") is None
    assert classify_api_error('{"type": "authentication_error"}') is None


@pytest.mark.parametrize("status", [429, 500, 529])
def test_a_stated_status_names_the_same_kind_its_wording_would(status: int) -> None:
    """A harness that records the status as a field states the same fact as one that words it,
    so the two entry points must not drift apart."""
    assert kind_for_status(status) == classify_api_error(f"API Error: {status} something")


@pytest.mark.parametrize("status", [None, 502])
def test_a_status_we_do_not_name_yields_no_kind(status: int | None) -> None:
    """A failure can carry no status at all, or one outside the table; both leave the kind
    unnamed rather than guessing one."""
    assert kind_for_status(status) is None


def test_the_auth_family_is_left_to_the_caller_here() -> None:
    """The asymmetry with `classify_api_error`, which screens the auth vocabulary out: this
    function sees a number and no text, so it cannot tell a permission failure from a rejected
    credential. 401 is simply absent from the table, and 403 is named -- callers that read a
    status must settle the auth question before consulting it, or a credential dead end loses
    its sign-in action to a bare `permission`."""
    assert kind_for_status(401) is None
    assert kind_for_status(403) == "permission"


def test_ordinary_assistant_text_is_not_an_error() -> None:
    assert classify_api_error("Here's the fix. The bug was in the auth middleware.") is None
    assert classify_api_error("") is None


def test_none_kind_is_not_a_provider_fault() -> None:
    assert is_provider_fault(None) is False


def test_pi_bare_status_form_is_classified() -> None:
    """pi writes no `API Error:` prefix -- just the status and the body."""
    assert classify_api_error('529 {"type":"error","error":{"type":"overloaded_error"}}') == "overloaded"


def test_a_status_quoted_mid_message_is_not_a_failure() -> None:
    """The bare form is anchored to the start of the string. Unanchored, any message
    mentioning a status code would render as a failed turn."""
    assert classify_api_error("Retry with 500 tokens and see if 429 shows up again") is None


def test_auth_wins_so_the_two_subtexts_cannot_stack() -> None:
    """Anthropic reports exhausted third-party usage as a 400 `invalid_request_error`, which
    is in BOTH this module's type table and the auth vocabulary. Classifying it here as well
    would put two contradictory next steps under one message."""
    assert classify_api_error('400 {"type":"invalid_request_error","message":"credit balance is too low"}') is None
