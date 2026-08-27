"""Remote service connector: the Modal deployment entrypoint.

Exposes authenticated HTTP endpoints for managing remote services used by the
minds desktop client (pool-host leasing, LiteLLM keys, R2 buckets, workspace
sync, self-hosted workspace sharing, SuperTokens-backed authentication). The endpoints and
business logic live in this package's modules; this file holds ONLY what Modal
needs at deploy time: the image, the app, the secrets, and the function
definitions (web app + crons).

This file is deployed by file path (``modal deploy app.py``), so Modal ships
just this file (as top-level module ``app``) plus the packages added via
``add_local_python_source`` below (this package and ``imbue.modal_app_kit``).
Anything else from the monorepo must NOT be imported by the shipped modules --
it would work locally and crash the container at import time. This file itself
is excluded from the package source mount, so package modules can never import
``imbue.remote_service_connector.app``. See libs/modal_app_kit/README.md for
the full deployment model.

Secrets are environment-scoped so the same code can back a production,
staging, or ad-hoc deploy without editing this file. ``MNGR_DEPLOY_ENV`` is
resolved at local ``modal deploy`` time from the deployer's shell and used to
select the correct ``<service>-<tier>-<deploy_id>`` Modal secrets. The same
values are also baked into an inline secret so the running container can read
them at request time (see ``/version``).
"""

import functools
import logging
import os
from pathlib import Path

import modal
from fastapi import FastAPI
from supertokens_python.framework.fastapi import get_middleware as get_supertokens_middleware

import imbue.remote_service_connector.cloudflare as cloudflare_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.r2.stores as r2_stores_module
import imbue.remote_service_connector.stop_start as stop_start_module
import imbue.remote_service_connector.sync as sync_module
from imbue.modal_app_kit.deploy import deploy_metadata_secret
from imbue.modal_app_kit.deploy import read_custom_domains
from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.deploy import read_deploy_id
from imbue.modal_app_kit.deploy import read_min_containers
from imbue.modal_app_kit.deploy import read_scaledown_window
from imbue.modal_app_kit.deploy import stamped_secret
from imbue.modal_app_kit.image import locate_image_requirements
from imbue.modal_app_kit.image import pinned_image
from imbue.modal_app_kit.request_logging import RequestLoggingMiddleware
from imbue.modal_app_kit.request_logging import deployed_minds_env_name
from imbue.modal_app_kit.sentry import capture_and_reraise
from imbue.modal_app_kit.sentry import init_sentry
from imbue.modal_app_kit.source_mount import shipped_python_source_ignore
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth_proxy import EnsureAsgiRootPathMiddleware
from imbue.remote_service_connector.auth_proxy import PartitionedCookieMiddleware
from imbue.remote_service_connector.auth_proxy import init_supertokens
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.hosts import reconcile_slice_boxes
from imbue.remote_service_connector.r2.sweep import run_r2_quota_sweep
from imbue.remote_service_connector.relay_health import get_dns_record_set_ops
from imbue.remote_service_connector.relay_health import probe_relay_healthz
from imbue.remote_service_connector.relay_health import run_relay_health_sweep
from imbue.remote_service_connector.relays import get_relay_store
from imbue.remote_service_connector.retention import run_backup_retention_reap
from imbue.remote_service_connector.stop_start import run_transition_supervisor
from imbue.remote_service_connector.stop_start import run_transition_watchdog
from imbue.remote_service_connector.web import web_app

logger = logging.getLogger(__name__)


_DEPLOY_ENV = read_deploy_env()

# Per-deploy timestamp baked into the deployed function spec by ``minds
# env deploy`` so the connector pins to the matching ``<svc>-<tier>-<id>``
# Modal Secrets. See ``read_deploy_id`` for the unset-sentinel safety
# property the timestamped-secret rollback model needs.
_MINDS_DEPLOY_ID = read_deploy_id()

# Warm-pool size for the deployed function. ``minds-admin env deploy`` reads
# the tier's ``[min_containers].connector`` from its committed
# ``deploy.toml`` and threads the value here at ``modal deploy`` time --
# which is when this module is imported and the function spec is
# serialized. Defaults to 0 so a deploy that forgets to set the env var
# gets the cheapest possible warm pool (cold start on first hit).
_MIN_CONTAINERS = read_min_containers("MINDS_CONNECTOR_MIN_CONTAINERS")

# How long (seconds) an idle container stays alive before Modal scales it
# down. ``minds-admin env deploy`` threads the tier's
# ``[scaledown_window].connector`` from its committed ``deploy.toml`` here
# at ``modal deploy`` time. Dev tiers set this high (~10 min) so the
# no-warm-pool connector stays hot across a dev session instead of
# cold-booting on every request; staging / production leave it unset and
# rely on ``min_containers`` instead. None (from the unset/0 default, the
# ci/test tier) means "don't pin it" -- the function falls back to Modal's
# own default scaledown window.
_SCALEDOWN_WINDOW = read_scaledown_window("MINDS_CONNECTOR_SCALEDOWN_WINDOW")

# Modal custom domains for the web function (the tier's user-facing accounts +
# web-chrome hosts). ``minds-admin env deploy`` threads the tier's ``[origins]``
# hosts from its committed ``deploy.toml`` here at ``modal deploy`` time.
# Every named domain must already be registered and verified in the deploying
# Modal workspace (dashboard -> Domains) or the deploy fails. None (the
# default) deploys with only the ``*.modal.run`` URL -- dev/ci tiers and any
# deploy outside the wrapper.
_CUSTOM_DOMAINS = read_custom_domains("MINDS_CONNECTOR_CUSTOM_DOMAINS")

# The `service` tag / server_name Bugsink events carry, distinguishing this
# app from the other reporters on the tier's shared instance.
_SENTRY_SERVICE_NAME = "remote-service-connector"

# All build steps (the hash-locked pip install onto the digest-pinned base --
# see ``imbue.modal_app_kit.image``) come first and are cached; local source is
# attached as the single final operation. With the default copy=False it is a
# container-startup mount, not an image layer, so code changes never
# invalidate the image cache (Modal enforces the ordering). The entrypoint
# (this file) ships separately via Modal's automatic file mount and is
# excluded from the package mount by ``shipped_python_source_ignore``.
#
# The built accounts frontend bundle (login/signup/account pages) is attached
# the same way: ``minds-admin env deploy`` runs the Vite build before ``modal
# deploy``, and the dist directory rides along as a container-startup mount at
# the path ``accounts_web.frontend_dist_dir`` reads. The directory may be
# absent on a bare ``modal deploy`` from a checkout that never built it -- the
# accounts pages then serve a 503 placeholder rather than failing the deploy.
_ACCOUNTS_FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"
_WEB_CHROME_FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend_web" / "dist"
_base_image = pinned_image(locate_image_requirements(Path(__file__)))
if _ACCOUNTS_FRONTEND_DIST.is_dir():
    _base_image = _base_image.add_local_dir(_ACCOUNTS_FRONTEND_DIST, remote_path="/root/accounts_frontend_dist")
if _WEB_CHROME_FRONTEND_DIST.is_dir():
    _base_image = _base_image.add_local_dir(_WEB_CHROME_FRONTEND_DIST, remote_path="/root/web_chrome_frontend_dist")
image = _base_image.add_local_python_source(
    "imbue.remote_service_connector",
    "imbue.modal_app_kit",
    ignore=shipped_python_source_ignore,
)
app = modal.App(name=f"rsc-{_DEPLOY_ENV}", image=image)


def _connector_secrets() -> list[modal.Secret]:
    """The Modal secrets attached to every connector function (web app + cron)."""
    return [
        stamped_secret("cloudflare", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("supertokens", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("neon", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("pool-ssh", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("litellm-connector", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("sharing", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("storage", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("sentry", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        deploy_metadata_secret(_DEPLOY_ENV, _MINDS_DEPLOY_ID),
    ]


@app.function(
    name="api",
    secrets=_connector_secrets(),
    # Warm-pool size driven by ``_MIN_CONTAINERS`` at the top of this
    # module: defaults to 1 for production / staging (avoid cold-boot
    # penalty on auth / lease / share hits from the desktop client) and
    # 0 for dev (per-developer envs sit idle most of the time). Override
    # at deploy time with ``MINDS_CONNECTOR_MIN_CONTAINERS=<n>``. Mirrors the
    # equivalent block in apps/modal_litellm/app.py.
    min_containers=_MIN_CONTAINERS,
    # Idle-before-scaledown window driven by ``_SCALEDOWN_WINDOW`` (already
    # None when unset, so Modal uses its own default); dev pins this high so
    # the no-warm-pool connector stays hot across a dev session.
    scaledown_window=_SCALEDOWN_WINDOW,
)
# Without this, Modal delivers ONE request per container at a time, so a
# single slow request (a lease's SSH provisioning, a cold sync pull) makes
# every other caller queue behind it or wait out a fresh container's cold
# boot -- even with a warm pool. The app is safe to run concurrently: routes
# are sync ``def`` (FastAPI runs them on its threadpool), every route opens
# its own psycopg2 connection and closes it in ``finally``, the lease
# selection uses ``FOR UPDATE SKIP LOCKED``, the shared Cloudflare
# ``httpx.Client`` is thread-safe, and the only module-level mutable state
# (the paid-status cache) is lock-guarded. ``max_inputs`` is kept modest
# because each concurrent request holds one direct Neon connection and one
# threadpool thread for its duration.
@modal.concurrent(max_inputs=8)
@modal.asgi_app(custom_domains=_CUSTOM_DOMAINS)
def fastapi_app() -> FastAPI:
    # Error reporting to the tier's Bugsink instance; a no-op until the
    # tier's `sentry` Vault entry carries RSC_SENTRY_DSN. Initialized before
    # anything that can fail so startup errors are reported too.
    init_sentry(_SENTRY_SERVICE_NAME, "RSC_SENTRY_DSN")
    init_supertokens()
    # The SuperTokens middleware serves the accounts surface's browser-session
    # machinery (cookie attachment, the refresh route under
    # ACCOUNTS_AUTH_API_BASE_PATH). Added here -- after init, before the first
    # request -- because it resolves the initialized SDK instance per request;
    # unit tests exercise web_app without it via the accounts_web seams.
    # Guarded on the same condition init_supertokens() no-ops on: the
    # middleware calls Supertokens.get_instance() on EVERY request and raises
    # when init never ran, which would turn an unconfigured deployment's
    # graceful 503s (require_supertokens_configured) into 500s on all routes.
    if os.environ.get("SUPERTOKENS_CONNECTION_URI"):
        web_app.add_middleware(get_supertokens_middleware())
        # Added after (outside) the SuperTokens middleware: root_path first, then
        # Modal's ASGI shim omits root_path from the scope and the SuperTokens
        # middleware raises on every request without it (see
        # EnsureAsgiRootPathMiddleware).
        web_app.add_middleware(EnsureAsgiRootPathMiddleware)
        # Added last = outermost, so its response-header rewrite runs after the
        # SuperTokens middleware has attached its SameSite=None session cookies,
        # appending the CHIPS Partitioned attribute the SDK cannot emit itself.
        web_app.add_middleware(PartitionedCookieMiddleware)
    # Outermost of all: one structured access-log line per request (client IP,
    # method, path, status, duration -- no query strings or bodies), so abuse
    # investigations have a per-request record in the Modal function logs.
    web_app.add_middleware(RequestLoggingMiddleware)
    return web_app


@app.function(
    name="cleanup_removing_pool_hosts",
    secrets=_connector_secrets(),
    # Hourly slice-box reconcile audit. Scoped to this env's stamped slices; it
    # only alerts (never auto-deletes), so it is safe on a box shared by multiple
    # dev envs.
    schedule=modal.Cron("0 * * * *"),
    timeout=900,
)
def cleanup_removing_pool_hosts() -> dict[str, int]:
    init_sentry(_SENTRY_SERVICE_NAME, "RSC_SENTRY_DSN")
    with capture_and_reraise():
        return _cleanup_removing_pool_hosts()


def _cleanup_removing_pool_hosts() -> dict[str, int]:
    conn = db.get_pool_db_connection()
    try:
        # Audit this env's slices on every box against the DB (alert-only: it never
        # auto-deletes, to avoid racing an in-flight bake). Scoped to MINDS_ENV_NAME so
        # it is safe on a box shared by multiple dev envs. A reconcile failure (DB,
        # SSH, or a missing POOL_SSH_PRIVATE_KEY while boxes exist) is a real failure:
        # let it propagate and fail the cron run rather than silently swallowing it.
        divergence_count = reconcile_slice_boxes(conn, deployed_minds_env_name())
    finally:
        conn.close()
    logger.info("Slice reconcile done: slice_divergences=%d", divergence_count)
    return {"slice_divergences": divergence_count}


# One-time-per-container SuperTokens init for the sweep cron: the sweep's lazy
# entitlements creation resolves owner emails via the SuperTokens SDK, and
# ``supertokens_init`` must not run twice in a warm container.
@functools.cache
def _init_supertokens_once() -> None:
    init_supertokens()


@app.function(
    name="r2_quota_sweep",
    secrets=_connector_secrets(),
    # Hourly storage-quota sweep, offset from the slice reconcile so the two
    # crons don't contend for a cold container at the top of the hour.
    schedule=modal.Cron("30 * * * *"),
    timeout=900,
)
def r2_quota_sweep() -> dict[str, int]:
    init_sentry(_SENTRY_SERVICE_NAME, "RSC_SENTRY_DSN")
    with capture_and_reraise():
        return _r2_quota_sweep()


def _r2_quota_sweep() -> dict[str, int]:
    _init_supertokens_once()
    counters = run_r2_quota_sweep(
        cloudflare_module.get_cloudflare_ctx().ops,
        r2_stores_module.get_key_store(),
        entitlements_module.get_entitlements_store(),
        r2_stores_module.get_grant_store(),
    )
    logger.info("R2 quota sweep done: %s", counters)
    return counters


@app.function(
    name="backup_retention_reap",
    secrets=_connector_secrets(),
    # Hourly destroyed-workspace backup reap, offset from the other crons.
    # Work is bounded per pass (record + object budgets) and resumable, so a
    # single invocation never approaches the timeout.
    schedule=modal.Cron("15 * * * *"),
    timeout=900,
)
def backup_retention_reap() -> dict[str, int]:
    init_sentry(_SENTRY_SERVICE_NAME, "RSC_SENTRY_DSN")
    with capture_and_reraise():
        return _backup_retention_reap()


def _backup_retention_reap() -> dict[str, int]:
    counters = run_backup_retention_reap(
        cloudflare_module.get_cloudflare_ctx().ops,
        sync_module.get_sync_store(),
        r2_stores_module.get_key_store(),
        sync_module.get_orphan_bucket_store(),
    )
    logger.info("Backup retention reap done: %s", counters)
    return counters


@app.function(
    name="relay_health_sweep",
    secrets=_connector_secrets(),
    # Every-minute relay liveness sweep: probes each active relay's /healthz
    # and keeps the region DNS record sets in step (2 consecutive failures pull
    # an IP, 1 success restores, never below the full active set). Health only
    # steers visitors -- tunnels keep connecting to unhealthy relays so they
    # serve again the moment they recover. Cheap: a handful of HTTP probes and
    # (only on drift) a few Cloudflare record edits.
    schedule=modal.Cron("* * * * *"),
    cpu=0.25,
    memory=512,
    timeout=120,
)
def relay_health_sweep() -> dict[str, int]:
    init_sentry(_SENTRY_SERVICE_NAME, "RSC_SENTRY_DSN")
    with capture_and_reraise():
        return _relay_health_sweep()


def _relay_health_sweep() -> dict[str, int]:
    # A tier with relays registered but no sharing config is a deploy mistake;
    # skip (visibly) rather than crash-loop the cron every minute.
    try:
        counters = run_relay_health_sweep(get_relay_store(), get_dns_record_set_ops, probe_relay_healthz)
    except MissingShareConfigError as exc:
        logger.warning("Skipping relay health sweep (sharing not configured): %s", exc)
        return {"skipped": 1}
    logger.info("Relay health sweep done: %s", counters)
    return counters


@app.function(
    name="workspace_transition_supervisor",
    secrets=_connector_secrets(),
    # One supervisor drives one workspace's stop/start transition end to end.
    # It only SSH-polls a box status file every ~15s and finalizes DB state,
    # so it runs on the smallest resource footprint Modal offers; the 2h
    # timeout bounds even a badly throttled upload, after which the hourly
    # watchdog re-drives the row.
    cpu=0.25,
    memory=512,
    timeout=7200,
)
def workspace_transition_supervisor(host_db_id: str, transition_id: str) -> str:
    init_sentry(_SENTRY_SERVICE_NAME, "RSC_SENTRY_DSN")
    with capture_and_reraise():
        return _workspace_transition_supervisor(host_db_id, transition_id)


def _workspace_transition_supervisor(host_db_id: str, transition_id: str) -> str:
    outcome = run_transition_supervisor(host_db_id, transition_id)
    logger.info("Transition supervisor for %s finished: %s", host_db_id, outcome)
    return outcome


# The stop/start endpoints (and the watchdog) spawn supervisors through this
# hook. Wired here because only the entrypoint may import ``modal``: the
# shipped modules hold the seam, the entrypoint provides the implementation.
def _spawn_transition_supervisor(host_db_id: str, transition_id: str) -> None:
    workspace_transition_supervisor.spawn(host_db_id, transition_id)


stop_start_module.spawner.hook = _spawn_transition_supervisor


@app.function(
    name="workspace_transition_watchdog",
    secrets=_connector_secrets(),
    # Hourly watchdog for orphaned transitions: rows stuck in stopping/starting
    # (or stopped-with-a-leftover-VM) whose supervisor heartbeat went stale
    # (connector redeploy, Modal eviction, supervisor timeout) are taken over
    # under a fresh fencing token and re-driven, with an exponential backoff
    # in the transition's consecutive-failure count and an ops alert once a
    # transition has clearly stopped converging.
    schedule=modal.Cron("45 * * * *"),
    timeout=900,
)
def workspace_transition_watchdog() -> dict[str, int]:
    init_sentry(_SENTRY_SERVICE_NAME, "RSC_SENTRY_DSN")
    with capture_and_reraise():
        return _workspace_transition_watchdog()


def _workspace_transition_watchdog() -> dict[str, int]:
    redriven_count = run_transition_watchdog()
    logger.info("Transition watchdog done: redriven=%d", redriven_count)
    return {"redriven": redriven_count}
