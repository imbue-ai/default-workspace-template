"""FastAPI app assembly: mounts every feature router and owns the system endpoints.

The system endpoints (liveness, generation, version) are unauthenticated
operational probes used by ``minds-admin env deploy`` and the deployment-test
suite; everything else lives in the feature routers.
"""

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import HTTPException

import imbue.remote_service_connector.shares as shares_module
from imbue.modal_app_kit.deploy import DEPLOY_ID_ENV_VAR
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector.accounts import router as accounts_router
from imbue.remote_service_connector.accounts_web import router as accounts_web_router
from imbue.remote_service_connector.auth_proxy import router as auth_proxy_router
from imbue.remote_service_connector.errors import ReportingProbeError
from imbue.remote_service_connector.hosts import router as hosts_router
from imbue.remote_service_connector.http_api import handle_unexpected_exception
from imbue.remote_service_connector.http_api import is_exception_detail_exposed
from imbue.remote_service_connector.lease_records import router as lease_records_router
from imbue.remote_service_connector.llm_keys import router as llm_keys_router
from imbue.remote_service_connector.pool_gauges import router as pool_gauges_router
from imbue.remote_service_connector.r2.buckets import router as r2_buckets_router
from imbue.remote_service_connector.r2.grants import router as r2_grants_router
from imbue.remote_service_connector.relays import router as relays_router
from imbue.remote_service_connector.retention import router as retention_router
from imbue.remote_service_connector.share_broker import router as share_broker_router
from imbue.remote_service_connector.share_certs import router as share_certs_router
from imbue.remote_service_connector.shares import router as shares_router
from imbue.remote_service_connector.suspension_admin import router as suspension_admin_router
from imbue.remote_service_connector.sync import router as sync_router
from imbue.remote_service_connector.workspaces import router as workspaces_router

logger = logging.getLogger(__name__)


# The ``async`` is mandated by Starlette's lifespan protocol (the same
# protocol-shim exception as the ASGI middlewares' ``async __call__``).
@asynccontextmanager
async def _flush_metrics_on_shutdown(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Modal container scaledown is a graceful ASGI shutdown, so the ping
    # metrics buffered since the last periodic flush are emitted here instead
    # of dying with the container.
    shares_module.flush_frps_ping_metrics()


web_app = FastAPI(lifespan=_flush_metrics_on_shutdown)
# Exceptions no layer expected (neither the routes nor raise_as_http's
# domain mapping) land here: reported to Bugsink at top priority, answered
# with the generic internal_error body. See http_api.handle_unexpected_exception.
web_app.add_exception_handler(Exception, handle_unexpected_exception)
web_app.include_router(auth_proxy_router)
web_app.include_router(accounts_web_router)
web_app.include_router(sync_router)
web_app.include_router(hosts_router)
web_app.include_router(workspaces_router)
web_app.include_router(r2_buckets_router)
web_app.include_router(r2_grants_router)
web_app.include_router(shares_router)
web_app.include_router(relays_router)
web_app.include_router(share_certs_router)
web_app.include_router(share_broker_router)
web_app.include_router(llm_keys_router)
web_app.include_router(accounts_router)
web_app.include_router(suspension_admin_router)
web_app.include_router(retention_router)
web_app.include_router(lease_records_router)
web_app.include_router(pool_gauges_router)


# Public env var name the deployed connector reads at startup to expose
# the tier's generation id via ``GET /generation``. The id is minted by
# ``minds-admin env deploy`` and stored in HCP Vault at
# ``secrets/minds/<tier>/generation``; the per-tier ``litellm-connector-<tier>``
# Modal Secret carries it into the container. See
# ``apps/minds_admin/imbue/minds_admin/envs/generation.py`` for the full lifecycle.
# An empty string is the **steady state** for any tier whose
# ``deploy.toml`` has ``[lifecycle].tracks_generation = false`` (dev tier
# today) -- ``deploy_env`` only mints + pushes a generation id when the
# flag is true, so the connector sees no value and ``/generation``
# answers ``{"generation_id": ""}``. The activate-time auto-wipe in
# ``minds-admin env activate`` skips the wipe on empty, which is the right
# no-op for untracked tiers. (Empty is also what an older pre-generation-
# lifecycle deploy would produce, hence the matching legacy fallback.)
_GENERATION_ID_ENV_VAR = "MINDS_TIER_GENERATION_ID"

# Test-only env var honored by ``/health/liveness``. When set to ``"1"``,
# the liveness probe returns 500 unconditionally so the deployment-test
# suite can drive the auto-rollback path in ``minds-admin env deploy`` without
# editing source. Unset in every non-test deploy. See
# ``specs/minds-deployment-tests.md`` (``test_deploy_auto_rollback_on_broken_healthcheck``).
_INJECT_BROKEN_HEALTHCHECK_ENV_VAR = "MINDS_INJECT_BROKEN_HEALTHCHECK"


@web_app.get("/health/liveness")
def get_health_liveness() -> dict[str, str]:
    """Lightweight no-auth liveness probe.

    Used by ``minds-admin env deploy``'s post-deploy health check to confirm
    the connector is reachable. Returns a fixed body so the poller has
    something to assert on beyond a 200 status.

    Honors ``MINDS_INJECT_BROKEN_HEALTHCHECK=1`` per-request so the
    deployment-test suite can drive the auto-rollback flow. The env
    var is unset in every non-test deploy.
    """
    if os.environ.get(_INJECT_BROKEN_HEALTHCHECK_ENV_VAR) == "1":
        raise HTTPException(status_code=500, detail="liveness probe failed: MINDS_INJECT_BROKEN_HEALTHCHECK=1")
    return {"status": "ok"}


# The reporting-probe marker is client input that ends up inside a log line
# and an exception message; reduce it to a short, safe token.
_REPORTING_PROBE_MARKER_RE = re.compile(r"[^A-Za-z0-9-]")
_REPORTING_PROBE_MARKER_MAX_CHARS = 64


@web_app.get("/health/reporting-probe")
def get_reporting_probe(marker: str = "") -> dict[str, str]:
    """Deliberately exercise every reporting channel in one request (dev/ci only).

    Emits a metric record, logs a warning, and raises an unmapped
    :class:`ReportingProbeError` -- so one call proves the metric log line,
    the warning-level Bugsink event, and the unexpected-exception path (the
    app-level 500 handler and its ``internal_error`` response contract,
    including a non-empty ``event_id`` when the tier's DSN is plumbed). The
    deployment-test suite drives it with a unique ``marker`` it can find the
    resulting Bugsink events by.

    No-auth like the other health endpoints, but active only on the tiers
    where 500 bodies expose exception detail (dev/ci); on production and
    staging it answers ``{"status": "disabled"}`` and does nothing.
    """
    if not is_exception_detail_exposed():
        return {"status": "disabled"}
    safe_marker = _REPORTING_PROBE_MARKER_RE.sub("", marker)[:_REPORTING_PROBE_MARKER_MAX_CHARS]
    emit_metric("reporting_probe", 1, {})
    logger.warning("Reporting probe warning (marker=%s)", safe_marker)
    raise ReportingProbeError(f"reporting probe error (marker={safe_marker})")


@web_app.get("/generation")
def get_generation() -> dict[str, str]:
    """Return the tier generation id minted at ``minds-admin env deploy`` time.

    ``minds-admin env activate <tier>`` polls this on the client side: if the
    returned id differs from the per-env ``last_seen_generation``
    marker the dev has on disk, the tier has been destroyed + redeployed
    since they last activated, and local state needs to be wiped.

    Doesn't require auth -- the generation id is non-sensitive (just a
    uuid the operator can read off ``minds-admin env list`` or Vault anyway).
    """
    return {"generation_id": os.environ.get(_GENERATION_ID_ENV_VAR, "")}


@web_app.get("/version")
def get_version() -> dict[str, str]:
    """Return the connector's deploy id + tier generation id.

    Used by the deployment-test suite to assert that a re-deploy
    actually advances the live Modal app version (the ``deploy_id``
    field) and as part of the logged-in smoke test's "is this env
    healthy" sanity check.

    Reads two env vars that are already populated by ``minds-admin env
    deploy`` for every tier:

    * ``MINDS_DEPLOY_ID`` -- the compact ISO-8601 timestamp minted by
      ``secret_lifecycle.make_deploy_id`` and threaded through the
      Modal Secret bundle; advances on every successful deploy.
    * ``MINDS_TIER_GENERATION_ID`` -- the tier generation uuid;
      empty for tiers that don't track generations (dev today).

    No auth required (mirrors ``/generation`` -- the values are
    non-sensitive and surfaceable from any operator's machine via
    ``modal app describe``).
    """
    return {
        "deploy_id": os.environ.get(DEPLOY_ID_ENV_VAR, ""),
        "generation_id": os.environ.get(_GENERATION_ID_ENV_VAR, ""),
    }
