import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imbue.remote_service_connector.errors import ConnectorError
from imbue.remote_service_connector.errors import R2BucketNotFoundError
from imbue.remote_service_connector.http_api import INTERNAL_ERROR_MESSAGE
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.http_api import handle_unexpected_exception
from imbue.remote_service_connector.http_api import is_exception_detail_exposed
from imbue.remote_service_connector.http_api import raise_as_http
from imbue.remote_service_connector.web import web_app


class _UnmappedProbeError(ConnectorError):
    """A connector error deliberately absent from raise_as_http's mapping."""


_probe_app = FastAPI()
_probe_app.add_exception_handler(Exception, handle_unexpected_exception)


@_probe_app.get("/probe/unexpected")
def _route_raising_unexpected_error() -> dict[str, str]:
    with handle_endpoint_errors():
        raise _UnmappedProbeError("secret internal detail 4419")


@_probe_app.get("/probe/mapped")
def _route_raising_mapped_error() -> dict[str, str]:
    with handle_endpoint_errors():
        raise R2BucketNotFoundError("prefix--missing-bucket-8823")


def _probe_client() -> TestClient:
    return TestClient(_probe_app, raise_server_exceptions=False)


def test_unexpected_exception_returns_generic_internal_error_without_leaking(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "production")
    resp = _probe_client().get("/probe/unexpected")

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "internal_error"
    assert detail["message"] == INTERNAL_ERROR_MESSAGE
    assert "secret internal detail 4419" not in resp.text
    # Both optional-valued fields are always present so clients shaped against
    # dev/ci responses never break in production; empty here (no active sentry
    # client in unit tests, and no exposure on the production tier).
    assert detail["event_id"] == ""
    assert detail["exception"] == ""


def test_unexpected_exception_includes_exception_repr_on_dev_tier(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "dev")
    resp = _probe_client().get("/probe/unexpected")

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "internal_error"
    assert "secret internal detail 4419" in detail["exception"]
    assert "_UnmappedProbeError" in detail["exception"]


def test_mapped_domain_error_still_gets_its_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "production")
    resp = _probe_client().get("/probe/mapped")

    assert resp.status_code == 404
    assert "prefix--missing-bucket-8823" in resp.json()["detail"]


def test_is_exception_detail_exposed_fails_closed_without_a_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_DEPLOY_ENV", raising=False)
    assert is_exception_detail_exposed() is False


@pytest.mark.parametrize(
    ("tier", "is_exposed"),
    [("production", False), ("staging", False), ("dev", True), ("ci", True)],
)
def test_is_exception_detail_exposed_by_tier(monkeypatch: pytest.MonkeyPatch, tier: str, is_exposed: bool) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", tier)
    assert is_exception_detail_exposed() is is_exposed


def test_raise_as_http_reraises_the_original_unexpected_exception() -> None:
    original = _UnmappedProbeError("propagates unchanged 6634")
    with pytest.raises(_UnmappedProbeError) as raised:
        raise_as_http(original)
    assert raised.value is original


def test_web_app_registers_the_unexpected_exception_handler() -> None:
    """The production app must wire bare Exception to the 500 handler.

    The probe-app tests above cover the handler's behavior; this pins the one
    line in web.py that connects it to the deployed app.
    """
    assert web_app.exception_handlers[Exception] is handle_unexpected_exception
