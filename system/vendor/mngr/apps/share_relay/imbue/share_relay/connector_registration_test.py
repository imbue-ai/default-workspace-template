import httpx
import pytest

from imbue.share_relay.connector_registration import RelayRegistrationError
from imbue.share_relay.connector_registration import _check_admin_response


def test_check_admin_response_returns_the_object_body() -> None:
    body = _check_admin_response(httpx.Response(200, json={"relay_id": "relay-" + "e" * 16}))
    assert body == {"relay_id": "relay-" + "e" * 16}


def test_check_admin_response_raises_on_error_status() -> None:
    with pytest.raises(RelayRegistrationError, match="error 401"):
        _check_admin_response(httpx.Response(401, text="unauthorized"))


def test_check_admin_response_raises_on_non_json_response() -> None:
    # Edge proxies can answer 2xx with HTML or an empty body; that must surface
    # as a clear registration error, not an opaque JSON decode error.
    with pytest.raises(RelayRegistrationError, match="non-JSON response \\(status 200\\)"):
        _check_admin_response(httpx.Response(200, text="<html>bad gateway</html>"))


def test_check_admin_response_raises_on_non_object_body() -> None:
    with pytest.raises(RelayRegistrationError, match="non-object body"):
        _check_admin_response(httpx.Response(200, json=["not", "an", "object"]))
