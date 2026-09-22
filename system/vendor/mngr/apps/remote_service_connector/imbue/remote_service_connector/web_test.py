import pytest
from anyio import to_thread
from fastapi.testclient import TestClient

from imbue.remote_service_connector.deploy_constants import API_MAX_CONCURRENT_INPUTS
from imbue.remote_service_connector.deploy_constants import SYNC_ROUTE_THREAD_LIMIT
from imbue.remote_service_connector.web import web_app


def _client() -> TestClient:
    return TestClient(web_app, raise_server_exceptions=False)


def test_reporting_probe_is_disabled_on_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "production")
    resp = _client().get("/health/reporting-probe", params={"marker": "probe-marker-7181"})

    assert resp.status_code == 200
    assert resp.json() == {"status": "disabled"}


def test_reporting_probe_exercises_the_unexpected_error_path_on_dev(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "dev")
    resp = _client().get("/health/reporting-probe", params={"marker": "probe-marker-4437"})

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "internal_error"
    assert "probe-marker-4437" in detail["exception"]
    assert "ReportingProbeError" in detail["exception"]


def test_reporting_probe_sanitizes_a_hostile_marker(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "dev")
    resp = _client().get("/health/reporting-probe", params={"marker": 'evil"\n{}$(cmd) 9954'})

    assert resp.status_code == 500
    exception_text = resp.json()["detail"]["exception"]
    assert "marker=evilcmd9954" in exception_text


def test_lifespan_raises_the_sync_route_thread_limit_above_the_input_cap() -> None:
    """Requests Modal admits into the container must never queue for a worker thread."""
    with TestClient(web_app) as client:
        # The limiter is per event loop, so it has to be read on the loop the
        # lifespan ran on: the client's portal.
        assert client.portal is not None
        applied_limit = client.portal.call(to_thread.current_default_thread_limiter).total_tokens

    assert applied_limit == SYNC_ROUTE_THREAD_LIMIT
    assert applied_limit > API_MAX_CONCURRENT_INPUTS
