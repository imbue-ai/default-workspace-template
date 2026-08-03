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
    eval "$(uv run minds env activate production)"
    uv run minds env deploy --yes-i-mean-production

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
from imbue.modal_app_kit.source_mount import shipped_python_source_ignore

_DEPLOY_ENV = read_deploy_env()

# Per-deploy timestamp baked into the deployed function spec. ``minds env
# deploy`` mints this at the start of every deploy and threads it through
# the ``modal deploy`` subprocess env. The deployed function pins to the
# matching ``<svc>-<tier>-<MINDS_DEPLOY_ID>`` Modal Secrets, so
# ``modal app rollback`` reverts the captured env and re-attaches to the
# previous deploy's secrets in one shot. See ``read_deploy_id`` for the
# unset-sentinel safety property.
_MINDS_DEPLOY_ID = read_deploy_id()

# Warm-pool size for the deployed function. ``minds env deploy`` reads
# the tier's ``[min_containers].litellm_proxy`` from its committed
# ``deploy.toml`` and threads the value here at ``modal deploy`` time --
# which is when this module is imported and the function spec is
# serialized. Defaults to 0 so a deploy that forgets to set the env
# var gets the cheapest possible warm pool (cold start on first hit).
_MIN_CONTAINERS = read_min_containers("MINDS_LITELLM_PROXY_MIN_CONTAINERS")

# Idle-before-scaledown window (seconds). ``minds env deploy`` threads the
# tier's ``[scaledown_window].litellm_proxy`` here at ``modal deploy`` time.
# Dev tiers set this high (~10 min) so the no-warm-pool proxy stays hot
# across a dev session; staging / production leave it unset and rely on
# ``min_containers``. None (from the unset/0 default, the ci/test tier)
# means "don't pin it" -- Modal uses its own default.
_SCALEDOWN_WINDOW = read_scaledown_window("MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW")

# Per-token USD pricing for each Anthropic model, mirrored verbatim from
# litellm's model_prices_and_context_window map. We register pricing inline
# (via litellm_params) rather than relying on litellm's bundled price map so
# cost tracking stays correct even on litellm versions whose bundled map
# predates a model (e.g. claude-opus-4-8 only landed in litellm's price map
# in the 1.88.0 pre-release line). MUST stay in sync with
# litellm_proxy/config.yaml -- config_drift_test.py enforces this.
_FABLE_PRICING = {
    "input_cost_per_token": 0.00001,
    "output_cost_per_token": 0.00005,
    "cache_creation_input_token_cost": 0.0000125,
    "cache_read_input_token_cost": 0.000001,
}
_OPUS_PRICING = {
    "input_cost_per_token": 0.000005,
    "output_cost_per_token": 0.000025,
    "cache_creation_input_token_cost": 0.00000625,
    "cache_read_input_token_cost": 0.0000005,
}
# Opus 4.1 and the original Opus 4 (claude-opus-4-20250514) predate the Opus
# price drop and cost 3x the newer Opus models.
_OPUS_LEGACY_PRICING = {
    "input_cost_per_token": 0.000015,
    "output_cost_per_token": 0.000075,
    "cache_creation_input_token_cost": 0.00001875,
    "cache_read_input_token_cost": 0.0000015,
}
_SONNET_PRICING = {
    "input_cost_per_token": 0.000003,
    "output_cost_per_token": 0.000015,
    "cache_creation_input_token_cost": 0.00000375,
    "cache_read_input_token_cost": 0.0000003,
}
_HAIKU_PRICING = {
    "input_cost_per_token": 0.000001,
    "output_cost_per_token": 0.000005,
    "cache_creation_input_token_cost": 0.00000125,
    "cache_read_input_token_cost": 0.0000001,
}


def _model_entry(model_name: str, pricing: dict[str, float]) -> dict[str, object]:
    """Build a litellm model_list entry that forwards to the Anthropic API with inline pricing."""
    litellm_params: dict[str, object] = {
        "model": f"anthropic/{model_name}",
        "api_key": "os.environ/ANTHROPIC_API_KEY",
    }
    litellm_params.update(pricing)
    return {"model_name": model_name, "litellm_params": litellm_params}


LITELLM_CONFIG = {
    "model_list": [
        # Fable line.
        _model_entry("claude-fable-5", _FABLE_PRICING),
        # Current Opus line.
        _model_entry("claude-opus-4-8", _OPUS_PRICING),
        _model_entry("claude-opus-4-7", _OPUS_PRICING),
        _model_entry("claude-opus-4-6", _OPUS_PRICING),
        _model_entry("claude-opus-4-5", _OPUS_PRICING),
        # Older Opus (higher price tier), still active on the Anthropic API.
        _model_entry("claude-opus-4-1", _OPUS_LEGACY_PRICING),
        _model_entry("claude-opus-4-20250514", _OPUS_LEGACY_PRICING),
        # Sonnet line.
        _model_entry("claude-sonnet-4-6", _SONNET_PRICING),
        _model_entry("claude-sonnet-4-5", _SONNET_PRICING),
        _model_entry("claude-sonnet-4-20250514", _SONNET_PRICING),
        # Haiku line (bare alias + dated id both routable).
        _model_entry("claude-haiku-4-5", _HAIKU_PRICING),
        _model_entry("claude-haiku-4-5-20251001", _HAIKU_PRICING),
    ],
    "general_settings": {
        "database_url": "os.environ/DATABASE_URL",
        "master_key": "os.environ/LITELLM_MASTER_KEY",
    },
    "litellm_settings": {
        "drop_params": True,
        "num_retries": 0,
    },
}


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
    config_path = _write_config_file()
    os.environ["CONFIG_FILE_PATH"] = config_path
    os.environ["WORKER_CONFIG"] = json.dumps(
        {
            "config": config_path,
        }
    )

    from litellm.proxy.proxy_server import app as fastapi_app

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
        logging.info("Completed prisma db push:\n%s", combined_output)
        return
    if _is_connection_failure_output(combined_output):
        logging.warning("Failed to reach the database server during prisma db push (retryable):\n%s", combined_output)
        raise _PrismaConnectionError(combined_output)
    raise _PrismaMigrationError(f"prisma db push exited {result.returncode}:\n{combined_output}")


@app.function(
    secrets=[stamped_secret("litellm", _DEPLOY_ENV, _MINDS_DEPLOY_ID)],
    timeout=300,
)
def migrate_db() -> None:
    """Run `prisma db push` against DATABASE_URL to bring the LiteLLM schema current.

    Invoked by ``minds env deploy`` (via
    ``apps/minds/imbue/minds/envs/per_env_deploy.py::deploy_litellm_proxy``)
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
    import litellm.proxy

    logging.basicConfig(level=logging.INFO, force=True)
    direct_url = direct_database_url(os.environ["DATABASE_URL"])
    direct_host = urllib.parse.urlsplit(direct_url).hostname
    logging.info("Running prisma db push against database host %s", direct_host)
    schema_path = os.path.join(os.path.dirname(litellm.proxy.__file__), "schema.prisma")
    _run_prisma_db_push(schema_path, {**os.environ, "DATABASE_URL": direct_url})
