"""LiteLLM proxy deployed as a Modal serverless function.

This file is deployed by file path (``modal deploy app.py``), so Modal ships
just this file plus the packages added via ``add_local_python_source`` below
(today: ``imbue.modal_app_kit``, our shared Modal deploy conventions).
Anything else from the monorepo must NOT be imported here -- it would work
locally and crash the container at import time. See
libs/modal_app_kit/README.md for the deployment model.

LiteLLM's native ``POST /v1/messages`` route accepts the Anthropic API
request shape, so the Anthropic SDK / Claude Code can talk to the proxy
by setting ``ANTHROPIC_BASE_URL`` to the proxy's root URL (no path
suffix). The SDK appends ``/v1/messages`` itself. All requests go
through LiteLLM's virtual key system for cost tracking.

Usage:
    # Push secrets to Modal + deploy in one shot:
    eval "$(uv run minds-admin env activate production)"
    uv run minds-admin env deploy --yes-i-mean-production

    # Use with claude -p (replace with your virtual key and Modal URL)
    ANTHROPIC_BASE_URL=https://<workspace>--llm-production-proxy.modal.run/ \\
    ANTHROPIC_API_KEY=sk-your-virtual-key \\
    claude -p "hello"
"""

import json
import logging
import os
import subprocess
import urllib.parse
from pathlib import Path
from typing import Final

import modal
import tenacity

from imbue.modal_app_kit.database import direct_database_url
from imbue.modal_app_kit.deploy import deploy_metadata_secret
from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.deploy import read_deploy_id
from imbue.modal_app_kit.deploy import read_min_containers
from imbue.modal_app_kit.deploy import read_scaledown_window
from imbue.modal_app_kit.deploy import stamped_secret
from imbue.modal_app_kit.image import IMAGE_REQUIREMENTS_FILENAME
from imbue.modal_app_kit.image import pinned_image
from imbue.modal_app_kit.log_format import configure_logging
from imbue.modal_app_kit.request_logging import RequestLoggingMiddleware
from imbue.modal_app_kit.sentry import capture_and_reraise
from imbue.modal_app_kit.sentry import init_sentry
from imbue.modal_app_kit.sentry import resolve_sentry_dsn
from imbue.modal_app_kit.sentry import resolve_sentry_environment
from imbue.modal_app_kit.source_mount import shipped_python_source_ignore

# Named under the ``imbue`` subtree so the shared logging configuration's
# level knob covers this entrypoint's lines (the module itself is ``app``).
logger = logging.getLogger("imbue.modal_litellm.app")

_DEPLOY_ENV = read_deploy_env()

# Per-deploy timestamp baked into the deployed function spec.
# ``minds-admin env deploy`` mints this at the start of every deploy and threads it through
# the ``modal deploy`` subprocess env. The deployed function pins to the
# matching ``<svc>-<tier>-<MINDS_DEPLOY_ID>`` Modal Secrets, so
# ``modal app rollback`` reverts the captured env and re-attaches to the
# previous deploy's secrets in one shot. See ``read_deploy_id`` for the
# unset-sentinel safety property.
_MINDS_DEPLOY_ID = read_deploy_id()

# Warm-pool size for the deployed function. ``minds-admin env deploy`` reads
# the tier's ``[min_containers].litellm_proxy`` from its committed
# ``deploy.toml`` and threads the value here at ``modal deploy`` time --
# which is when this module is imported and the function spec is
# serialized. Defaults to 0 so a deploy that forgets to set the env
# var gets the cheapest possible warm pool (cold start on first hit).
_MIN_CONTAINERS = read_min_containers("MINDS_LITELLM_PROXY_MIN_CONTAINERS")

# Idle-before-scaledown window (seconds). ``minds-admin env deploy`` threads the
# tier's ``[scaledown_window].litellm_proxy`` here at ``modal deploy`` time.
# Dev tiers set this high (~10 min) so the no-warm-pool proxy stays hot
# across a dev session; staging / production leave it unset and rely on
# ``min_containers``. None (from the unset/0 default, the ci/test tier)
# means "don't pin it" -- Modal uses its own default.
_SCALEDOWN_WINDOW = read_scaledown_window("MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW")

# Every Claude model is routable through one pattern entry: a client's bare model
# name (``claude-opus-5``) matches ``claude-*`` and is forwarded upstream as
# ``anthropic/claude-<rest>``. The pattern is deliberately ``claude-*`` rather
# than a bare ``*``: this proxy holds only an Anthropic credential, so a non-Claude
# name should fail here as an unknown model rather than be forwarded to Anthropic
# and come back as a confusing upstream error. (An Anthropic model that does not
# start with ``claude-`` would need this pattern widened.) Pricing comes from litellm's own model-cost map, fetched
# remotely at startup (``LITELLM_LOCAL_MODEL_COST_MAP`` is deliberately unset), so
# a new Anthropic model is routable and priced the day litellm's map carries it,
# with no entry to add here.
#
# The map also carries dimensions an inline per-token price cannot express, and
# which the previous enumerated config therefore got wrong: the fast-mode premium
# (``provider_specific_entry.fast``, 2x on Opus 5 / 4.8), the regional uplift, and
# the 1-hour cache-write rate (``cache_creation_input_token_cost_above_1hr``, 2x
# base against the 1.25x 5-minute rate that a single inline field assumes).
LITELLM_CONFIG = {
    "model_list": [
        {
            "model_name": "claude-*",
            "litellm_params": {
                "model": "anthropic/claude-*",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
    ],
    "general_settings": {
        "database_url": "os.environ/DATABASE_URL",
        "master_key": "os.environ/LITELLM_MASTER_KEY",
    },
    "litellm_settings": {
        "drop_params": True,
        "num_retries": 0,
        # LiteLLM's native JSON logging (one line per record with a ``level``):
        # at config load it re-homes the root logger and its own loggers onto
        # one JSON handler. The ``JSON_LOGS`` env var exported by
        # ``_litellm_logging_env_updates`` covers the window between import
        # and config load.
        "json_logs": True,
        # LiteLLM's native Sentry integration: failed LLM calls are reported
        # (with LiteLLM's own context) to the tier's Bugsink instance via the
        # SENTRY_DSN env var, which litellm_app() maps from LITELLM_SENTRY_DSN.
        # Failure payloads can include request contents; the instance's short
        # retention is the compensating control (see
        # specs/minds-bugsink-error-tracking.md).
        "failure_callback": ["sentry"],
    },
}


def _litellm_sentry_env_updates(environ: dict[str, str]) -> dict[str, str]:
    """Env vars to export so LiteLLM's native sentry failure_callback behaves.

    On the first proxied LLM call, LiteLLM's ``set_callbacks`` re-runs
    ``sentry_sdk.init`` from env vars, REPLACING the global client our
    ``init_sentry`` installed -- so its knobs must be pinned via LiteLLM's own
    env vars: ``SENTRY_DSN`` (the DSN, resolved via the shared helper so the
    MINDS_SENTRY_DISABLED kill switch has one semantic across both reporting
    paths), ``SENTRY_API_TRACE_RATE=0.0`` (LiteLLM defaults to 1.0, which
    would send a performance transaction per HTTP request to the tiny
    single-container Bugsink instance), and ``SENTRY_ENVIRONMENT`` (LiteLLM
    defaults to the literal "production" regardless of tier). Values already
    present in ``environ`` (e.g. supplied through the stamped secret) win.
    Empty when reporting is disabled, so LiteLLM's re-init never activates.
    """
    dsn = resolve_sentry_dsn(environ, "LITELLM_SENTRY_DSN")
    if dsn is None:
        return {}
    updates = {"SENTRY_DSN": dsn}
    if "SENTRY_API_TRACE_RATE" not in environ:
        updates["SENTRY_API_TRACE_RATE"] = "0.0"
    if "SENTRY_ENVIRONMENT" not in environ:
        updates["SENTRY_ENVIRONMENT"] = resolve_sentry_environment(environ)
    return updates


def _litellm_logging_env_updates(environ: dict[str, str]) -> dict[str, str]:
    """Env vars to export before the proxy import so LiteLLM's own log lines carry a level.

    ``JSON_LOGS`` gives the handler LiteLLM attaches to its loggers at import
    its JSON formatter (``{"message", "level", "timestamp", ...}``).
    ``LITELLM_LOG`` is what the proxy's startup reads to set its own loggers'
    level: INFO matches our ``imbue.*`` packages (unset, they would inherit
    the root logger's WARNING and emit nothing else); DEBUG restores
    LiteLLM's verbose output. Values already present in ``environ`` (e.g.
    supplied through the stamped secret to debug a dev env) win.
    """
    updates: dict[str, str] = {}
    if "JSON_LOGS" not in environ:
        updates["JSON_LOGS"] = "1"
    if "LITELLM_LOG" not in environ:
        updates["LITELLM_LOG"] = "INFO"
    return updates


def _write_config_file() -> str:
    """Write the litellm config to a temp YAML file and return the path."""
    import yaml

    config_path = "/tmp/litellm_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(LITELLM_CONFIG, f)
    return config_path


# All build steps (the hash-locked pip install onto the digest-pinned base --
# see ``imbue.modal_app_kit.image`` -- then prisma codegen) come first and are
# cached; local source is attached as the single final operation. With the
# default copy=False it is a container-startup mount, not an image layer, so
# code changes never invalidate the image cache (Modal enforces the ordering).
# The pip set (including the deliberately-pinned litellm) lives in this app's
# ``[dependency-groups] image`` in pyproject.toml, exported to
# image_requirements.txt.
image = (
    pinned_image(Path(__file__).parent / IMAGE_REQUIREMENTS_FILENAME)
    # The prisma codegen step below downloads a current Node via nodeenv at
    # build time (a floating input the prisma layer already accepts -- it also
    # npm-installs the prisma CLI). Node 24+ links against libatomic, which
    # the slim base image does not ship, so new image builds fail with
    # "node: error while loading shared libraries: libatomic.so.1" without it
    # (cached images kept working, which is why this surfaced only on fresh
    # env deploys).
    .apt_install("libatomic1")
    .run_commands(
        'python -c "import litellm.proxy; import os; print(os.path.dirname(litellm.proxy.__file__))" > /tmp/litellm_proxy_dir.txt',
        "prisma generate --schema $(cat /tmp/litellm_proxy_dir.txt)/schema.prisma",
    )
    .add_local_python_source("imbue.modal_app_kit", ignore=shipped_python_source_ignore)
)

app = modal.App(name=f"llm-{_DEPLOY_ENV}", image=image)


@app.function(
    name="proxy",
    secrets=[
        stamped_secret("litellm", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("sentry", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        deploy_metadata_secret(_DEPLOY_ENV, _MINDS_DEPLOY_ID),
    ],
    min_containers=_MIN_CONTAINERS,
    # Idle-before-scaledown window driven by ``_SCALEDOWN_WINDOW`` (already
    # None when unset, so Modal uses its own default); dev pins this high so
    # the no-warm-pool proxy stays hot across a dev session.
    scaledown_window=_SCALEDOWN_WINDOW,
    timeout=600,
)
@modal.asgi_app()
def litellm_app():
    # JSON log lines for everything logged before LiteLLM takes over the root
    # logger at config load (see the ``json_logs`` setting) -- and for the
    # ``RequestLoggingMiddleware`` lines, whose dedicated handler LiteLLM's
    # re-homing never touches.
    configure_logging()
    # Server-level error reporting to the tier's Bugsink instance; a no-op
    # until the tier's `sentry` Vault entry carries LITELLM_SENTRY_DSN.
    init_sentry("litellm-proxy", "LITELLM_SENTRY_DSN")
    # LiteLLM reads its logging knobs (JSON_LOGS, LITELLM_LOG) and its sentry
    # failure_callback's literal SENTRY_DSN env var (plus the trace-rate /
    # environment knobs) at import; export them before the proxy import so
    # both initialize correctly. Note LiteLLM's sentry re-init replaces the
    # client configured by init_sentry above on the first LLM call, so this
    # process's server-level events lose the dedup before_send limiter and
    # the release/server_name labels -- an accepted consequence of using the
    # native callback (see _litellm_sentry_env_updates).
    os.environ.update(_litellm_logging_env_updates(dict(os.environ)))
    os.environ.update(_litellm_sentry_env_updates(dict(os.environ)))
    config_path = _write_config_file()
    os.environ["CONFIG_FILE_PATH"] = config_path
    os.environ["WORKER_CONFIG"] = json.dumps(
        {
            "config": config_path,
        }
    )

    from litellm.proxy.proxy_server import app as fastapi_app

    # Outermost middleware: one structured access-log line per request (client
    # IP, method, path, status, duration -- no query strings or bodies), so
    # abuse investigations have a per-request record in the Modal function logs.
    fastapi_app.add_middleware(RequestLoggingMiddleware)
    return fastapi_app


# Prisma error codes that mean the database server could not be reached at all
# (P1001: can't reach server, P1002: server reached but timed out, P1017:
# server closed the connection). These are the transient connect-path failures
# worth retrying (e.g. a network/DNS blip in the fresh Modal container); every
# other failure (auth, schema, migration state) must fail fast so the deploy's
# rollback fires on the first attempt.
_PRISMA_CONNECTION_ERROR_CODES: Final[tuple[str, ...]] = ("P1001", "P1002", "P1017")


class _PrismaMigrationError(Exception):
    """Raised when `prisma db push` fails for a non-connection reason."""


class _PrismaConnectionError(_PrismaMigrationError):
    """Raised when `prisma db push` could not reach the database server (retryable)."""


def _is_connection_failure_output(prisma_output: str) -> bool:
    return any(code in prisma_output for code in _PRISMA_CONNECTION_ERROR_CODES)


@tenacity.retry(
    retry=tenacity.retry_if_exception_type(_PrismaConnectionError),
    stop=tenacity.stop_after_attempt(5),
    wait=tenacity.wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _run_prisma_db_push(schema_path: str, subprocess_env: dict[str, str]) -> None:
    """Run one `prisma db push` attempt, retrying only on connection-class failures."""
    result = subprocess.run(
        ["prisma", "db", "push", "--schema", schema_path, "--accept-data-loss", "--skip-generate"],
        env=subprocess_env,
        capture_output=True,
        text=True,
    )
    combined_output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode == 0:
        logger.info("Completed prisma db push:\n%s", combined_output)
        return
    if _is_connection_failure_output(combined_output):
        logger.warning("Failed to reach the database server during prisma db push (retryable):\n%s", combined_output)
        raise _PrismaConnectionError(combined_output)
    raise _PrismaMigrationError(f"prisma db push exited {result.returncode}:\n{combined_output}")


@app.function(
    secrets=[
        stamped_secret("litellm", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        stamped_secret("sentry", _DEPLOY_ENV, _MINDS_DEPLOY_ID),
        # Supplies MNGR_DEPLOY_ENV + MINDS_DEPLOY_ID at runtime so Sentry
        # events from this function carry the real environment / release
        # instead of "unknown" (init_sentry reads both from os.environ).
        deploy_metadata_secret(_DEPLOY_ENV, _MINDS_DEPLOY_ID),
    ],
    timeout=300,
)
def migrate_db() -> None:
    """Run `prisma db push` against DATABASE_URL to bring the LiteLLM schema current.

    Invoked by ``minds-admin env deploy`` (via
    ``apps/minds_admin/imbue/minds_admin/envs/per_env_deploy.py::deploy_litellm_proxy``)
    before each ``modal deploy`` so the running proxy never sees a
    missing LiteLLM_VerificationToken / LiteLLM_BudgetTable / etc.

    Runs in the same image as the proxy itself, so prisma + the
    litellm[proxy] package (which ships the canonical schema.prisma)
    are already installed. Runs against the same `litellm-<tier>` Modal
    Secret the proxy consumes, so DATABASE_URL is necessarily the same
    Postgres the proxy will talk to at runtime -- except that the push
    itself connects over the DIRECT (non-``-pooler``) host, since schema
    operations are unsafe through transaction pooling (see
    ``imbue.modal_app_kit.database.direct_database_url``). Connection-class
    failures are retried with
    backoff (see ``_run_prisma_db_push``); real schema failures fail fast.

    Idempotent: prisma db push only applies diffs, so re-running on an
    already-current database is a no-op (~1s wall-clock). The
    --accept-data-loss flag is safe here -- the schema is LiteLLM's,
    not ours, so any "loss" would be of stale columns that LiteLLM
    itself dropped in a version bump (we don't write to those tables
    out-of-band). --skip-generate skips client codegen since the image
    already did that at build time.
    """
    configure_logging()
    # LiteLLM builds the handler for its own loggers from JSON_LOGS / LITELLM_LOG
    # at import, so they must be exported first (as in ``litellm_app``) or its
    # lines here would come out as colored text.
    os.environ.update(_litellm_logging_env_updates(dict(os.environ)))
    import litellm.proxy

    init_sentry("litellm-proxy", "LITELLM_SENTRY_DSN")
    with capture_and_reraise():
        direct_url = direct_database_url(os.environ["DATABASE_URL"])
        direct_host = urllib.parse.urlsplit(direct_url).hostname
        logger.info("Running prisma db push against database host %s", direct_host)
        schema_path = os.path.join(os.path.dirname(litellm.proxy.__file__), "schema.prisma")
        _run_prisma_db_push(schema_path, {**os.environ, "DATABASE_URL": direct_url})
