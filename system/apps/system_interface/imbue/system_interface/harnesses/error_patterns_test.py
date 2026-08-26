"""Unit tests for the Claude API-error classifier."""

from imbue.system_interface.harnesses.error_patterns import classify_api_error
from imbue.system_interface.harnesses.error_patterns import is_provider_fault


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


def test_ordinary_assistant_text_is_not_an_error() -> None:
    assert classify_api_error("Here's the fix. The bug was in the auth middleware.") is None
    assert classify_api_error("") is None


def test_none_kind_is_not_a_provider_fault() -> None:
    assert is_provider_fault(None) is False


def test_bare_status_form_is_classified() -> None:
    # pi surfaces a failure with no "API Error:" prefix -- just the status then the JSON.
    assert classify_api_error('529 {"type":"error","error":{"type":"overloaded_error"}}') == "overloaded"
    assert classify_api_error("503 Service Unavailable") == "overloaded"


def test_bare_status_is_anchored_to_the_start() -> None:
    # A status merely mentioned mid-sentence is routine in a coding chat and must not
    # be read as a failure.
    assert classify_api_error("the docs say a 529 means overloaded") is None
