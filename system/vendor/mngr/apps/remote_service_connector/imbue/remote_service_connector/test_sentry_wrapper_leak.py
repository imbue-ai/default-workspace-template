"""Regression test for the sentry-sdk x FastAPI per-request wrapper leak (mngr-internal#493).

sentry-sdk < 2.63.0 re-wrapped ``dependant.call`` in place on every request to
a sync (``def``) endpoint served through FastAPI's lazy router inclusion, so a
warm container died with RecursionError (HTTP 500) after ~990 requests to one
endpoint -- which took down the staging connector's ``GET /workspaces``. Every
connector router is mounted via ``include_router`` and most endpoints are sync,
which is exactly the failing shape. This test pins the fixed behavior by
hammering a minimal included sync route well past the old failure threshold;
on a leaking SDK it dies with RecursionError around request ~990.
"""

from uuid import uuid4

import pytest
import sentry_sdk
from fastapi import APIRouter
from fastapi import FastAPI
from fastapi.testclient import TestClient

from imbue.modal_app_kit.sentry import init_sentry

# The old leak crossed Python's default recursion limit (1000) at ~990
# requests; run comfortably past that so a reintroduced leak cannot slip
# under the limit.
_HAMMER_REQUEST_COUNT = 1500


# The 1500-request loop legitimately takes several seconds with coverage
# enabled, too close to the project's default 10s per-test timeout on a
# loaded worker. A reintroduced leak fails via RecursionError, not timeout.
@pytest.mark.timeout(60)
def test_sync_endpoint_on_included_router_survives_many_requests_with_sentry_active(
    monkeypatch: pytest.MonkeyPatch, isolated_sentry_client: None
) -> None:
    # Activate the real SDK exactly like a deployed container does:
    # auto-enabling integrations installs the FastAPI/Starlette patches.
    # The DSN is fake and never contacted -- no events are captured below.
    dsn_env_var = f"RSC_LEAK_TEST_SENTRY_DSN_{uuid4().hex.upper()}"
    monkeypatch.setenv(dsn_env_var, f"https://{uuid4().hex}@bugsink.invalid/1")
    init_sentry(f"wrapper-leak-test-{uuid4().hex}", dsn_env_var)
    assert sentry_sdk.get_client().is_active()

    # A minimal app in the failing shape: a sync endpoint on an included router.
    router = APIRouter()

    @router.get("/sync-probe")
    def sync_probe() -> dict[str, int]:
        return {"ok": 1}

    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        for request_idx in range(_HAMMER_REQUEST_COUNT):
            response = client.get("/sync-probe")
            assert response.status_code == 200, f"request {request_idx} answered {response.status_code}"
