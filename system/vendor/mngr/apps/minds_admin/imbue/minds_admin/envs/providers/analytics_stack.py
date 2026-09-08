"""Provision / tear down a per-env analytics stack (dev-tier auto-bringup).

Tiers whose deploys create their own resources (``creates_resources=true``:
every personal dev env) get the analytics resources provisioned automatically
on the first ``minds-admin env deploy --with-analytics``, exactly like the
env's Neon project and SuperTokens app. Shared tiers (staging / production)
keep the operator bringup runbook + per-tier Vault entry
(``apps/analytics/docs/bringup.md``).

One stack per env, all deterministically named so orphans are reapable
without the env's local state:

* Neon project ``analytics-<env>`` with the ``metrics`` / ``transcripts`` /
  ``ops`` databases (see ``neon_db.create_analytics_neon_project``).
* R2 buckets ``analytics-metrics-<env>`` / ``analytics-transcripts-<env>``,
  each with one bucket-scoped readwrite account token named
  ``analytics-<kind>-<env>-rw``.
* One read-only account token ``analytics-logs-<env>-ro`` on the tier's
  shared OpenObserve bucket (the aggregation reads the log parquet; per-env
  isolation of the *content* comes from the ``minds_env`` field stamped into
  the log lines plus the ``ANALYTICS_LOGS_ENV_FILTER`` secret value).
* An ``analytics_reader`` read-only role on the env's own ``host_pool``
  database (per-env project, so the name cannot collide across envs).

Token minting is not naturally idempotent (the secret is only derivable at
mint time), so re-provisioning ROTATES: any existing account token with the
target name is deleted before a fresh one is minted. The resulting values are
persisted into the env's local ``secrets.toml`` by the deploy flow; a re-run
with complete persisted values skips this module entirely.
"""

import secrets as py_secrets
import urllib.parse
from collections.abc import Mapping
from typing import Any
from typing import Final

import httpx
import psycopg2
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import info_span
from imbue.imbue_common.pure import pure
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError
from imbue.minds_admin.envs.providers.neon_db import create_analytics_neon_project
from imbue.minds_admin.envs.providers.neon_db import delete_analytics_neon_project
from imbue.minds_admin.envs.providers.neon_db import direct_dsn_from_pooled
from imbue.minds_admin.envs.r2_cleanup import CloudflareR2Credentials
from imbue.minds_admin.envs.r2_cleanup import delete_bucket
from imbue.minds_admin.envs.r2_cleanup import empty_bucket
from imbue.minds_admin.envs.r2_cleanup import list_r2_buckets
from imbue.minds_admin.envs.r2_cleanup import mint_r2_object_token
from imbue.minds_admin.envs.r2_cleanup import revoke_token
from imbue.minds_admin.envs.r2_cleanup import wait_for_s3_credentials
from imbue.mngr_imbue_cloud.r2_objects import derive_s3_secret_from_token_value
from imbue.mngr_imbue_cloud.r2_objects import make_r2_s3_client

_CLOUDFLARE_API_BASE: Final[str] = "https://api.cloudflare.com/client/v4"
_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
_POSTGRES_TIMEOUT_SECONDS: Final[int] = 60

R2_READ_PERMISSION_GROUP_NAME: Final[str] = "Workers R2 Storage Bucket Item Read"
R2_WRITE_PERMISSION_GROUP_NAME: Final[str] = "Workers R2 Storage Bucket Item Write"

# The read-only role the collection/aggregation loop uses on the env's own
# connector (host_pool) database. Per-env Neon projects make the name safe.
_ANALYTICS_READER_ROLE: Final[str] = "analytics_reader"

# Hard bound on the account-token listing walk (50 tokens per page). The
# account also holds every user-bucket key the connector mints, but 10k
# tokens is far past any real account; running off the end only means a
# rotation misses tokens that could not exist.
_MAX_TOKEN_LIST_PAGES: Final[int] = 200


class AnalyticsStackError(MindError):
    """Raised when the per-env analytics stack cannot be provisioned or torn down."""


class AnalyticsStackRequest(FrozenModel):
    """Everything :func:`create_analytics_stack` needs to provision one env's stack."""

    name: DevEnvName = Field(description="The dev env the stack belongs to (drives every resource name)")
    neon_org_id: str = Field(description="Neon org the analytics-<env> project is created under")
    neon_api_token: SecretStr = Field(description="Neon API token with project-create scope on that org")
    cloudflare_account_id: str = Field(description="Cloudflare account holding the tier's R2 buckets")
    cloudflare_api_token: SecretStr = Field(description="Account-owned Cloudflare token (R2 + account-token edit)")
    logs_bucket: str = Field(description="The tier's shared OpenObserve R2 bucket (read-only source)")
    host_pool_admin_dsn: SecretStr = Field(description="Admin DSN of the env's host_pool database (may be pooled)")


class AnalyticsStackRecord(FrozenModel):
    """The provisioned per-env analytics resources (persisted into the env's secrets.toml)."""

    metrics_catalog_dsn: SecretStr = Field(description="Direct DSN of the metrics DuckLake catalog database")
    transcripts_catalog_dsn: SecretStr = Field(description="Direct DSN of the transcripts DuckLake catalog database")
    ops_dsn: SecretStr = Field(description="Direct DSN of the analytics ops database")
    rsc_readonly_dsn: SecretStr = Field(description="analytics_reader DSN on the env's host_pool database (direct)")
    metrics_bucket: str = Field(description="R2 bucket holding the metrics lake's parquet data")
    metrics_access_key_id: str = Field(description="S3 access key id scoped to the metrics bucket (readwrite)")
    metrics_secret_access_key: SecretStr = Field(description="S3 secret for the metrics bucket key")
    transcripts_bucket: str = Field(description="R2 bucket holding the transcripts lake's parquet data")
    transcripts_access_key_id: str = Field(description="S3 access key id scoped to the transcripts bucket (readwrite)")
    transcripts_secret_access_key: SecretStr = Field(description="S3 secret for the transcripts bucket key")
    logs_bucket: str = Field(description="The tier's OpenObserve R2 bucket (read-only source)")
    logs_access_key_id: str = Field(description="S3 access key id scoped to the OpenObserve bucket (read-only)")
    logs_secret_access_key: SecretStr = Field(description="S3 secret for the OpenObserve bucket key")
    r2_account_id: str = Field(description="Cloudflare account id the buckets live under")


# The env's local secrets.toml carries exactly these keys (a subset of the
# analytics Modal-secret schema in .minds/template/analytics.sh); the
# deploy-time-computed keys (env filter, collection tuning) are NOT persisted.
_LOCAL_SECRET_KEY_BY_FIELD: Final[dict[str, str]] = {
    "metrics_catalog_dsn": "ANALYTICS_METRICS_CATALOG_URL",
    "transcripts_catalog_dsn": "ANALYTICS_TRANSCRIPTS_CATALOG_URL",
    "ops_dsn": "ANALYTICS_OPS_DATABASE_URL",
    "rsc_readonly_dsn": "ANALYTICS_RSC_READONLY_DATABASE_URL",
    "metrics_bucket": "ANALYTICS_METRICS_R2_BUCKET",
    "metrics_access_key_id": "ANALYTICS_METRICS_R2_ACCESS_KEY_ID",
    "metrics_secret_access_key": "ANALYTICS_METRICS_R2_SECRET_ACCESS_KEY",
    "transcripts_bucket": "ANALYTICS_TRANSCRIPTS_R2_BUCKET",
    "transcripts_access_key_id": "ANALYTICS_TRANSCRIPTS_R2_ACCESS_KEY_ID",
    "transcripts_secret_access_key": "ANALYTICS_TRANSCRIPTS_R2_SECRET_ACCESS_KEY",
    "logs_bucket": "ANALYTICS_LOGS_R2_BUCKET",
    "logs_access_key_id": "ANALYTICS_LOGS_R2_ACCESS_KEY_ID",
    "logs_secret_access_key": "ANALYTICS_LOGS_R2_SECRET_ACCESS_KEY",
    "r2_account_id": "ANALYTICS_R2_ACCOUNT_ID",
}


@pure
def metrics_bucket_name_for(name: DevEnvName) -> str:
    return f"analytics-metrics-{name}"


@pure
def transcripts_bucket_name_for(name: DevEnvName) -> str:
    return f"analytics-transcripts-{name}"


@pure
def analytics_token_names_for(name: DevEnvName) -> tuple[str, str, str]:
    """The three account-token names one env's stack owns (metrics-rw, transcripts-rw, logs-ro).

    Deterministic so a cleanup sweep can find an env's tokens without its
    local state: revoke everything matching ``analytics-*-<env>-r[wo]`` whose
    env no longer exists.
    """
    return (
        f"analytics-metrics-{name}-rw",
        f"analytics-transcripts-{name}-rw",
        f"analytics-logs-{name}-ro",
    )


@pure
def local_secret_values_from_record(record: AnalyticsStackRecord) -> dict[str, SecretStr]:
    """The record as ``secrets.toml``-ready values (every value a SecretStr)."""
    value_by_field = record.model_dump()
    values: dict[str, SecretStr] = {}
    for field_name, secret_key in _LOCAL_SECRET_KEY_BY_FIELD.items():
        value = value_by_field[field_name]
        values[secret_key] = value if isinstance(value, SecretStr) else SecretStr(str(value))
    return values


@pure
def record_from_local_secrets(secrets: Mapping[str, SecretStr]) -> AnalyticsStackRecord | None:
    """Rebuild the persisted stack record from the env's secrets.toml, or None when incomplete.

    An env that never provisioned analytics has none of the keys; a partially
    persisted set (a deploy that died mid-write) also returns None so the next
    deploy re-provisions (token rotation makes that safe).
    """
    field_values: dict[str, str] = {}
    for field_name, secret_key in _LOCAL_SECRET_KEY_BY_FIELD.items():
        value = secrets.get(secret_key)
        if value is None or not value.get_secret_value():
            return None
        field_values[field_name] = value.get_secret_value()
    return AnalyticsStackRecord(
        metrics_catalog_dsn=SecretStr(field_values["metrics_catalog_dsn"]),
        transcripts_catalog_dsn=SecretStr(field_values["transcripts_catalog_dsn"]),
        ops_dsn=SecretStr(field_values["ops_dsn"]),
        rsc_readonly_dsn=SecretStr(field_values["rsc_readonly_dsn"]),
        metrics_bucket=field_values["metrics_bucket"],
        metrics_access_key_id=field_values["metrics_access_key_id"],
        metrics_secret_access_key=SecretStr(field_values["metrics_secret_access_key"]),
        transcripts_bucket=field_values["transcripts_bucket"],
        transcripts_access_key_id=field_values["transcripts_access_key_id"],
        transcripts_secret_access_key=SecretStr(field_values["transcripts_secret_access_key"]),
        logs_bucket=field_values["logs_bucket"],
        logs_access_key_id=field_values["logs_access_key_id"],
        logs_secret_access_key=SecretStr(field_values["logs_secret_access_key"]),
        r2_account_id=field_values["r2_account_id"],
    )


@pure
def analytics_secret_values_from_record(
    record: AnalyticsStackRecord,
    *,
    logs_env_filter: str,
    collection_interval_seconds: int | None,
) -> dict[str, str]:
    """Compose the analytics Modal-secret values for a per-env stack.

    ``logs_env_filter`` scopes the aggregation's log views to this env's own
    service log lines (the tier's OpenObserve bucket is shared across dev
    envs). ``collection_interval_seconds`` overrides the per-workspace
    collection interval; dev envs run it hot so a test workspace is
    re-collectable within minutes.
    """
    value_by_field = record.model_dump()
    values: dict[str, str] = {}
    for field_name, secret_key in _LOCAL_SECRET_KEY_BY_FIELD.items():
        value = value_by_field[field_name]
        values[secret_key] = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    values["ANALYTICS_LOGS_ENV_FILTER"] = logs_env_filter
    if collection_interval_seconds is not None:
        values["ANALYTICS_COLLECTION_INTERVAL_SECONDS"] = str(collection_interval_seconds)
    return values


@pure
def build_reader_dsn(admin_dsn: str, *, role: str, password: str) -> str:
    """A reader's DSN: the admin DSN's direct host + database, with ``role``'s credentials."""
    parsed = urllib.parse.urlsplit(direct_dsn_from_pooled(admin_dsn))
    _userinfo, _at_sign, host_and_port = parsed.netloc.rpartition("@")
    quoted_password = urllib.parse.quote(password, safe="")
    return urllib.parse.urlunsplit(parsed._replace(netloc=f"{role}:{quoted_password}@{host_and_port}"))


def _cloudflare_request(
    credentials: CloudflareR2Credentials, method: str, path: str, json_body: dict[str, Any] | None = None
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {credentials.api_token.get_secret_value()}"}
    try:
        response = httpx.request(
            method,
            f"{_CLOUDFLARE_API_BASE}{path}",
            headers=headers,
            json=json_body,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise AnalyticsStackError(f"Cloudflare {method} {path} failed: {e}") from e
    try:
        body = response.json()
    except ValueError as e:
        raise AnalyticsStackError(f"Cloudflare {method} {path} returned non-JSON: {response.text[:200]}") from e
    if not isinstance(body, dict) or not body.get("success"):
        raise AnalyticsStackError(f"Cloudflare {method} {path} failed: {response.text[:400]}")
    return body


def _ensure_bucket(credentials: CloudflareR2Credentials, bucket_name: str) -> None:
    """Create the bucket, tolerating an already-existing one (idempotent re-deploys)."""
    try:
        _cloudflare_request(
            credentials, "POST", f"/accounts/{credentials.account_id}/r2/buckets", {"name": bucket_name}
        )
    except AnalyticsStackError as e:
        # Cloudflare answers 10004 ("The bucket you tried to create already
        # exists...") on a name we already own; any other failure is real.
        if "10004" not in str(e) and "already exists" not in str(e):
            raise
        logger.info("Adopted pre-existing R2 bucket {!r}", bucket_name)


def permission_group_id(credentials: CloudflareR2Credentials, group_name: str) -> str:
    """The account's permission-group id for ``group_name`` (e.g. the R2 read/write groups)."""
    body = _cloudflare_request(credentials, "GET", f"/accounts/{credentials.account_id}/tokens/permission_groups")
    for group in body.get("result", []):
        if isinstance(group, dict) and group.get("name") == group_name:
            return str(group["id"])
    raise AnalyticsStackError(f"Cloudflare has no {group_name!r} permission group")


class CloudflareAccountToken(FrozenModel):
    """One account-owned Cloudflare API token, as returned by the tokens listing."""

    token_id: str = Field(description="The token's id (doubles as the S3 access key id for R2 tokens)")
    name: str = Field(description="The token's human-assigned name")


def list_cloudflare_account_tokens(credentials: CloudflareR2Credentials) -> list[CloudflareAccountToken]:
    """Every account token's (id, name), paginating the full list.

    The account also holds every user-bucket key the connector mints, so the
    listing can span many pages; any name filtering happens client-side
    because the tokens listing has no server-side name filter.
    """
    tokens: list[CloudflareAccountToken] = []
    for page in range(1, _MAX_TOKEN_LIST_PAGES + 1):
        body = _cloudflare_request(
            credentials, "GET", f"/accounts/{credentials.account_id}/tokens?per_page=50&page={page}"
        )
        result = body.get("result") or []
        if not isinstance(result, list) or not result:
            break
        tokens.extend(
            CloudflareAccountToken(token_id=str(entry["id"]), name=str(entry.get("name", "")))
            for entry in result
            if isinstance(entry, dict) and entry.get("id")
        )
        if len(result) < 50:
            break
    return tokens


def list_account_token_ids_by_name(credentials: CloudflareR2Credentials, token_name: str) -> list[str]:
    """Every account token id whose name is exactly ``token_name``."""
    return [token.token_id for token in list_cloudflare_account_tokens(credentials) if token.name == token_name]


def replace_bucket_token(
    credentials: CloudflareR2Credentials,
    *,
    token_name: str,
    bucket_name: str,
    permission_group_id: str,
) -> tuple[str, SecretStr]:
    """Mint the named bucket-scoped account token, rotating any prior token of that name.

    Returns ``(access_key_id, secret_access_key)`` in R2's S3 convention: the
    key id is the token id and the secret is the SHA-256 hex of the token
    value. Rotation (delete-then-mint) keeps exactly one live token per name,
    so a re-provision after lost local state never accumulates orphans.
    """
    for stale_token_id in list_account_token_ids_by_name(credentials, token_name):
        logger.info("Rotating stale Cloudflare account token {!r} ({})", token_name, stale_token_id)
        _cloudflare_request(credentials, "DELETE", f"/accounts/{credentials.account_id}/tokens/{stale_token_id}")
    # The resource key mirrors Cloudflare's R2 bucket resource identifier;
    # "default" is the (default) jurisdiction.
    resource_key = f"com.cloudflare.edge.r2.bucket.{credentials.account_id}_default_{bucket_name}"
    policies = [
        {
            "effect": "allow",
            "permission_groups": [{"id": permission_group_id}],
            "resources": {resource_key: "*"},
        }
    ]
    result = _cloudflare_request(
        credentials,
        "POST",
        f"/accounts/{credentials.account_id}/tokens",
        {"name": token_name, "policies": policies},
    )["result"]
    token_id = str(result.get("id", ""))
    token_value = str(result.get("value", ""))
    if not token_id or not token_value:
        raise AnalyticsStackError(f"Cloudflare did not return an id + value for account token {token_name!r}")
    return token_id, SecretStr(derive_s3_secret_from_token_value(token_value))


def _ensure_reader_role(admin_dsn: SecretStr) -> SecretStr:
    """Create (or re-password) the analytics_reader role on the env's host_pool database.

    Returns the reader DSN. Re-provisioning rotates the password, matching the
    token-rotation semantics above -- the fresh credentials land in the env's
    local state and the analytics Modal secret in the same deploy.
    """
    password = py_secrets.token_urlsafe(24)
    direct_admin_dsn = direct_dsn_from_pooled(admin_dsn.get_secret_value())
    try:
        connection = psycopg2.connect(direct_admin_dsn, connect_timeout=_POSTGRES_TIMEOUT_SECONDS)
    except psycopg2.Error as e:
        raise AnalyticsStackError("Cannot connect to the env's host_pool database to create analytics_reader") from e
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            ensure_readonly_role_on_connected_database(cursor, _ANALYTICS_READER_ROLE, password)
    except psycopg2.Error as e:
        raise AnalyticsStackError("Could not create/refresh the analytics_reader role on host_pool") from e
    finally:
        connection.close()
    return SecretStr(build_reader_dsn(direct_admin_dsn, role=_ANALYTICS_READER_ROLE, password=password))


def ensure_readonly_role_on_connected_database(cursor: Any, role_name: str, password: str) -> None:
    """Create (or re-password) ``role_name`` and grant read-only access on the connected database.

    The shared grant set for every analytics read-only role (the env's
    ``analytics_reader`` and the per-analyst roles); it mirrors the manual
    runbook in ``apps/analytics/reports/README.md``. psycopg2 errors propagate
    for the caller to wrap in its own error type.
    """
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
    is_existing = cursor.fetchone() is not None
    # Role DDL takes no parameter placeholders; callers pass internally
    # derived role names (a module constant or a validated AnalystName) and
    # generated passwords, so the interpolation carries no external input.
    quoted_password = password.replace("'", "''")
    if is_existing:
        cursor.execute(f"ALTER ROLE {role_name} WITH LOGIN PASSWORD '{quoted_password}'")
    else:
        cursor.execute(f"CREATE ROLE {role_name} WITH LOGIN PASSWORD '{quoted_password}'")
    cursor.execute(f"GRANT CONNECT ON DATABASE {current_database_identifier(cursor)} TO {role_name}")
    cursor.execute(f"GRANT USAGE ON SCHEMA public TO {role_name}")
    cursor.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role_name}")
    cursor.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role_name}")


def current_database_identifier(cursor: Any) -> str:
    """The connected database's name as a quoted SQL identifier (for GRANT/REVOKE ... ON DATABASE)."""
    cursor.execute("SELECT current_database()")
    row = cursor.fetchone()
    database_name = str(row[0]) if row else ""
    if not database_name:
        raise AnalyticsStackError("Could not resolve the connected database's name for the role grant")
    # Neon database names are snake_case identifiers; quote defensively anyway.
    return '"' + database_name.replace('"', '""') + '"'


def create_analytics_stack(request: AnalyticsStackRequest) -> AnalyticsStackRecord:
    """Provision one env's analytics stack end to end (idempotent, rotating credentials).

    Raises :class:`AnalyticsStackError` when any resource cannot be
    provisioned; earlier-created resources are left in place (every step is
    adopt-or-create, so the next deploy converges rather than duplicating).
    """
    if not request.cloudflare_account_id or not request.cloudflare_api_token.get_secret_value():
        raise AnalyticsStackError(
            "Analytics is enabled for this env but the tier's cloudflare Vault entry has no "
            "CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN; the per-env analytics stack needs them "
            "to create its R2 buckets and tokens."
        )
    if not request.logs_bucket:
        raise AnalyticsStackError(
            "Analytics is enabled for this env but the tier's observability Vault entry has no "
            "OPENOBSERVE_R2_BUCKET; the aggregation's log views read the tier's OpenObserve parquet."
        )
    with info_span("Provisioning analytics Neon project for env {!r}", str(request.name)):
        neon_record = create_analytics_neon_project(
            request.name, org_id=request.neon_org_id, api_token=request.neon_api_token
        )

    cloudflare = CloudflareR2Credentials(
        account_id=request.cloudflare_account_id, api_token=request.cloudflare_api_token
    )
    metrics_bucket = metrics_bucket_name_for(request.name)
    transcripts_bucket = transcripts_bucket_name_for(request.name)
    metrics_token_name, transcripts_token_name, logs_token_name = analytics_token_names_for(request.name)
    with info_span("Provisioning analytics R2 buckets + tokens for env {!r}", str(request.name)):
        _ensure_bucket(cloudflare, metrics_bucket)
        _ensure_bucket(cloudflare, transcripts_bucket)
        write_group_id = permission_group_id(cloudflare, R2_WRITE_PERMISSION_GROUP_NAME)
        read_group_id = permission_group_id(cloudflare, R2_READ_PERMISSION_GROUP_NAME)
        metrics_key_id, metrics_secret = replace_bucket_token(
            cloudflare, token_name=metrics_token_name, bucket_name=metrics_bucket, permission_group_id=write_group_id
        )
        transcripts_key_id, transcripts_secret = replace_bucket_token(
            cloudflare,
            token_name=transcripts_token_name,
            bucket_name=transcripts_bucket,
            permission_group_id=write_group_id,
        )
        logs_key_id, logs_secret = replace_bucket_token(
            cloudflare, token_name=logs_token_name, bucket_name=request.logs_bucket, permission_group_id=read_group_id
        )

    with info_span("Ensuring the analytics_reader role on the env's host_pool database"):
        rsc_readonly_dsn = _ensure_reader_role(request.host_pool_admin_dsn)

    return AnalyticsStackRecord(
        metrics_catalog_dsn=neon_record.metrics_dsn,
        transcripts_catalog_dsn=neon_record.transcripts_dsn,
        ops_dsn=neon_record.ops_dsn,
        rsc_readonly_dsn=rsc_readonly_dsn,
        metrics_bucket=metrics_bucket,
        metrics_access_key_id=metrics_key_id,
        metrics_secret_access_key=metrics_secret,
        transcripts_bucket=transcripts_bucket,
        transcripts_access_key_id=transcripts_key_id,
        transcripts_secret_access_key=transcripts_secret,
        logs_bucket=request.logs_bucket,
        logs_access_key_id=logs_key_id,
        logs_secret_access_key=logs_secret,
        r2_account_id=request.cloudflare_account_id,
    )


def delete_analytics_stack(
    name: DevEnvName,
    *,
    neon_org_id: str,
    neon_api_token: SecretStr,
    cloudflare_account_id: str,
    cloudflare_api_token: SecretStr,
) -> None:
    """Tear down one env's analytics stack (idempotent; a never-provisioned env is a fast no-op).

    Deletes the ``analytics-<env>`` Neon project, empties + deletes both
    analytics buckets, and revokes the env's three account tokens (including
    the read-only one on the shared OpenObserve bucket -- that bucket itself
    is tier infrastructure and is never touched). The ``analytics_reader``
    role needs no explicit drop: it lives inside the env's own Neon project,
    which env destroy deletes wholesale right after this.
    """
    with info_span("Deleting analytics Neon project for env {!r}", str(name)):
        delete_analytics_neon_project(name, org_id=neon_org_id, api_token=neon_api_token)

    if not cloudflare_account_id or not cloudflare_api_token.get_secret_value():
        logger.warning(
            "Skipping analytics R2 teardown for env {!r}: the tier's cloudflare Vault entry is not "
            "populated. Any analytics-*-{} buckets/tokens must be cleaned up manually.",
            str(name),
            str(name),
        )
        return
    cloudflare = CloudflareR2Credentials(account_id=cloudflare_account_id, api_token=cloudflare_api_token)
    stack_buckets = {metrics_bucket_name_for(name), transcripts_bucket_name_for(name)}
    existing_buckets = [bucket.name for bucket in list_r2_buckets(cloudflare) if bucket.name in stack_buckets]
    if existing_buckets:
        # Emptying needs S3 credentials; mint the same short-lived
        # account-wide write token the orphaned-bucket sweep uses and revoke
        # it in the same breath.
        sweep_token_id, sweep_token_value = mint_r2_object_token(cloudflare)
        try:
            s3_client = make_r2_s3_client(
                s3_endpoint=f"https://{cloudflare_account_id}.r2.cloudflarestorage.com",
                access_key_id=sweep_token_id,
                secret_access_key=derive_s3_secret_from_token_value(sweep_token_value.get_secret_value()),
            )
            wait_for_s3_credentials(s3_client, existing_buckets[0])
            for bucket_name in existing_buckets:
                with info_span("Emptying + deleting analytics R2 bucket {!r}", bucket_name):
                    object_count = empty_bucket(s3_client, bucket_name)
                    delete_bucket(cloudflare, bucket_name)
                    logger.info("Deleted analytics bucket {!r} ({} object(s))", bucket_name, object_count)
        finally:
            revoke_token(cloudflare, sweep_token_id)

    for token_name in analytics_token_names_for(name):
        for token_id in list_account_token_ids_by_name(cloudflare, token_name):
            with info_span("Revoking analytics account token {!r}", token_name):
                _cloudflare_request(cloudflare, "DELETE", f"/accounts/{cloudflare_account_id}/tokens/{token_id}")
