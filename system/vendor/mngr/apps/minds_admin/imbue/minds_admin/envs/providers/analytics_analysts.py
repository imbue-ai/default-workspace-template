"""Per-analyst read-only access to a tier's analytics lakes (metrics + transcripts).

Analyst access is deliberately per-person so reads stay attributable (see
``apps/analytics/reports/README.md``): each analyst gets a dedicated
read-only Postgres role on the tier's DuckLake catalog databases plus one
read-only, bucket-scoped R2 token per lake. This module is the engine behind
``minds-admin analytics analyst add|remove|list``.

Everything is deterministically named from the analyst's handle -- role
``analyst_<name>``, tokens ``analytics-analyst-<name>-<lake>-ro`` -- so the
listing and removal need no state beyond the tier's own backends. Token
minting rotates (delete-then-mint, like the per-env stack provisioning), so
re-running ``add`` re-issues fresh credentials for the same analyst.
"""

import json
import secrets as py_secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import auto
from typing import Any
from typing import Final

import psycopg2
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.minds_admin.envs.providers.analytics_stack import R2_READ_PERMISSION_GROUP_NAME
from imbue.minds_admin.envs.providers.analytics_stack import build_reader_dsn
from imbue.minds_admin.envs.providers.analytics_stack import current_database_identifier
from imbue.minds_admin.envs.providers.analytics_stack import ensure_readonly_role_on_connected_database
from imbue.minds_admin.envs.providers.analytics_stack import list_account_token_ids_by_name
from imbue.minds_admin.envs.providers.analytics_stack import list_cloudflare_account_tokens
from imbue.minds_admin.envs.providers.analytics_stack import permission_group_id
from imbue.minds_admin.envs.providers.analytics_stack import replace_bucket_token
from imbue.minds_admin.envs.providers.neon_db import direct_dsn_from_pooled
from imbue.minds_admin.envs.r2_cleanup import CloudflareR2Credentials
from imbue.minds_admin.envs.r2_cleanup import revoke_token
from imbue.minds_admin.primitives import AnalystName

_POSTGRES_TIMEOUT_SECONDS: Final[int] = 60

ANALYST_ROLE_PREFIX: Final[str] = "analyst_"
_ANALYST_TOKEN_PREFIX: Final[str] = "analytics-analyst-"
_ANALYST_TOKEN_SUFFIX: Final[str] = "-ro"


class AnalyticsAnalystError(MindError):
    """Raised when an analyst's analytics access cannot be provisioned, listed, or revoked."""


class AnalyticsLake(LowerCaseStrEnum):
    """The two analytics DuckLake lakes an analyst can be granted.

    The lowercase values are wire-visible: they are embedded in the Cloudflare
    token names.
    """

    METRICS = auto()
    TRANSCRIPTS = auto()


class AnalyticsAnalystAdminContext(FrozenModel):
    """The tier-level backend credentials analyst management operates with."""

    env_name: str = Field(description="Activated env the credentials were resolved for (stamped into hand-offs)")
    metrics_catalog_owner_dsn: SecretStr = Field(description="Owner DSN of the metrics DuckLake catalog database")
    transcripts_catalog_owner_dsn: SecretStr = Field(
        description="Owner DSN of the transcripts DuckLake catalog database"
    )
    metrics_bucket: str = Field(description="R2 bucket holding the metrics lake's parquet data")
    transcripts_bucket: str = Field(description="R2 bucket holding the transcripts lake's parquet data")
    r2_account_id: str = Field(description="Cloudflare account id the buckets live under")
    cloudflare_api_token: SecretStr = Field(description="Account-owned Cloudflare token (R2 + account-token edit)")


class LakeCredentials(FrozenModel):
    """One analyst's read-only credentials for one lake."""

    catalog_dsn: SecretStr = Field(description="Direct read-only DSN of the lake's DuckLake catalog database")
    r2_bucket: str = Field(description="R2 bucket holding the lake's parquet data")
    r2_access_key_id: str = Field(description="S3 access key id scoped read-only to the bucket")
    r2_secret_access_key: SecretStr = Field(description="S3 secret for the bucket key")


class AnalystCredentials(FrozenModel):
    """Everything one analyst needs to attach the lakes, as returned by provisioning."""

    analyst_name: AnalystName = Field(description="The analyst's handle")
    role_name: str = Field(description="The analyst's Postgres role on the catalog databases")
    r2_account_id: str = Field(description="Cloudflare account id (needed by DuckDB's R2 secret)")
    metrics: LakeCredentials = Field(description="Read-only credentials for the metrics lake")
    transcripts: LakeCredentials | None = Field(
        description="Read-only credentials for the transcripts lake (None when opted out)"
    )


class AnalystListing(FrozenModel):
    """One analyst's current access state, reconstructed from the backends."""

    analyst_name: str = Field(description="The analyst's handle (parsed from the role / token names)")
    is_role_present: bool = Field(description="Whether the analyst_<name> Postgres role exists")
    is_metrics_granted: bool = Field(description="Whether the role holds table grants on the metrics catalog")
    is_transcripts_granted: bool = Field(description="Whether the role holds table grants on the transcripts catalog")
    metrics_token_count: int = Field(description="Live R2 tokens named for this analyst on the metrics bucket")
    transcripts_token_count: int = Field(description="Live R2 tokens named for this analyst on the transcripts bucket")


class AnalystRemovalReport(FrozenModel):
    """What ``revoke_analyst_access`` actually removed (everything is idempotent)."""

    is_role_dropped: bool = Field(description="Whether the Postgres role existed and was dropped")
    revoked_token_count: int = Field(description="How many R2 tokens were revoked")


@pure
def analyst_role_name(name: AnalystName) -> str:
    return f"{ANALYST_ROLE_PREFIX}{name}"


@pure
def analyst_token_name(name: AnalystName, lake: AnalyticsLake) -> str:
    return f"{_ANALYST_TOKEN_PREFIX}{name}-{lake.value}{_ANALYST_TOKEN_SUFFIX}"


@pure
def analyst_name_from_role_name(role_name: str) -> str | None:
    """The analyst handle a role name encodes, or None for non-analyst roles."""
    if not role_name.startswith(ANALYST_ROLE_PREFIX):
        return None
    suffix = role_name[len(ANALYST_ROLE_PREFIX) :]
    return suffix if suffix else None


@pure
def analyst_name_and_lake_from_token_name(token_name: str) -> tuple[str, AnalyticsLake] | None:
    """The (analyst handle, lake) a token name encodes, or None for non-analyst tokens."""
    if not token_name.startswith(_ANALYST_TOKEN_PREFIX) or not token_name.endswith(_ANALYST_TOKEN_SUFFIX):
        return None
    middle = token_name[len(_ANALYST_TOKEN_PREFIX) : -len(_ANALYST_TOKEN_SUFFIX)]
    for lake in AnalyticsLake:
        lake_suffix = f"-{lake.value}"
        if middle.endswith(lake_suffix) and len(middle) > len(lake_suffix):
            return middle[: -len(lake_suffix)], lake
    return None


def _connect(owner_dsn: SecretStr) -> Any:
    direct_dsn = direct_dsn_from_pooled(owner_dsn.get_secret_value())
    try:
        connection = psycopg2.connect(direct_dsn, connect_timeout=_POSTGRES_TIMEOUT_SECONDS)
    except psycopg2.Error as e:
        raise AnalyticsAnalystError("Cannot connect to the analytics catalog database") from e
    connection.autocommit = True
    return connection


@contextmanager
def _catalog_cursor(owner_dsn: SecretStr, failure_message: str) -> Iterator[Any]:
    """A cursor on the catalog database, wrapping any psycopg2 error in :class:`AnalyticsAnalystError`."""
    connection = _connect(owner_dsn)
    try:
        with connection.cursor() as cursor:
            yield cursor
    except psycopg2.Error as e:
        raise AnalyticsAnalystError(failure_message) from e
    finally:
        connection.close()


def _ensure_role_with_readonly_grants(owner_dsn: SecretStr, role_name: str, password: str) -> None:
    """Create (or re-password) the role and grant read-only access on the connected database.

    The role is cluster-wide within the tier's analytics Neon project, so the
    second lake's call finds it already created and only refreshes the
    password (to the same value) and the per-database grants. The grants are
    the shared analytics read-only set (see
    :func:`ensure_readonly_role_on_connected_database`).
    """
    failure_message = f"Could not create/refresh the {role_name!r} role on the catalog database"
    with _catalog_cursor(owner_dsn, failure_message) as cursor:
        ensure_readonly_role_on_connected_database(cursor, role_name, password)


def _revoke_readonly_grants(owner_dsn: SecretStr, role_name: str) -> None:
    """Reverse :func:`_ensure_role_with_readonly_grants` on the connected database (no-op for absent roles).

    Explicit per-grant revokes (rather than ``DROP OWNED BY``) so the
    statement set exactly mirrors what was granted and needs no membership in
    the target role.
    """
    failure_message = f"Could not revoke the {role_name!r} role's grants on the catalog database"
    with _catalog_cursor(owner_dsn, failure_message) as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if cursor.fetchone() is None:
            return
        cursor.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {role_name}")
        cursor.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_name}")
        cursor.execute(f"REVOKE USAGE ON SCHEMA public FROM {role_name}")
        cursor.execute(f"REVOKE CONNECT ON DATABASE {current_database_identifier(cursor)} FROM {role_name}")


def _drop_role_if_exists(owner_dsn: SecretStr, role_name: str) -> bool:
    failure_message = f"Could not drop the {role_name!r} role (are its per-database grants revoked?)"
    with _catalog_cursor(owner_dsn, failure_message) as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
        if cursor.fetchone() is None:
            return False
        cursor.execute(f"DROP ROLE {role_name}")
        return True


def _cloudflare_credentials(context: AnalyticsAnalystAdminContext) -> CloudflareR2Credentials:
    return CloudflareR2Credentials(account_id=context.r2_account_id, api_token=context.cloudflare_api_token)


def _delete_tokens_named(cloudflare: CloudflareR2Credentials, token_name: str) -> int:
    token_ids = list_account_token_ids_by_name(cloudflare, token_name)
    for token_id in token_ids:
        revoke_token(cloudflare, token_id)
    return len(token_ids)


def provision_analyst_access(
    context: AnalyticsAnalystAdminContext,
    name: AnalystName,
    # False removes any existing transcripts access, making add authoritative
    # for the analyst's desired state.
    is_transcripts_included: bool,
) -> AnalystCredentials:
    """Grant (or re-issue) one analyst's read-only lake access, rotating their credentials.

    Raises :class:`AnalyticsAnalystError` when any backend step fails; steps
    already completed are left in place (every step is idempotent, so a re-run
    converges).
    """
    role_name = analyst_role_name(name)
    password = py_secrets.token_urlsafe(24)
    with log_span("Ensuring the {} role + read-only grants on the metrics catalog", role_name):
        _ensure_role_with_readonly_grants(context.metrics_catalog_owner_dsn, role_name, password)
    if is_transcripts_included:
        with log_span("Ensuring the {} role + read-only grants on the transcripts catalog", role_name):
            _ensure_role_with_readonly_grants(context.transcripts_catalog_owner_dsn, role_name, password)
    else:
        with log_span("Removing any transcripts access for {} (--no-transcripts)", role_name):
            _revoke_readonly_grants(context.transcripts_catalog_owner_dsn, role_name)

    cloudflare = _cloudflare_credentials(context)
    read_group_id = permission_group_id(cloudflare, R2_READ_PERMISSION_GROUP_NAME)
    with log_span("Minting the read-only metrics-bucket token for {}", str(name)):
        metrics_key_id, metrics_secret = replace_bucket_token(
            cloudflare,
            token_name=analyst_token_name(name, AnalyticsLake.METRICS),
            bucket_name=context.metrics_bucket,
            permission_group_id=read_group_id,
        )
    transcripts: LakeCredentials | None = None
    if is_transcripts_included:
        with log_span("Minting the read-only transcripts-bucket token for {}", str(name)):
            transcripts_key_id, transcripts_secret = replace_bucket_token(
                cloudflare,
                token_name=analyst_token_name(name, AnalyticsLake.TRANSCRIPTS),
                bucket_name=context.transcripts_bucket,
                permission_group_id=read_group_id,
            )
        transcripts = LakeCredentials(
            catalog_dsn=SecretStr(
                build_reader_dsn(
                    context.transcripts_catalog_owner_dsn.get_secret_value(), role=role_name, password=password
                )
            ),
            r2_bucket=context.transcripts_bucket,
            r2_access_key_id=transcripts_key_id,
            r2_secret_access_key=transcripts_secret,
        )
    else:
        _delete_tokens_named(cloudflare, analyst_token_name(name, AnalyticsLake.TRANSCRIPTS))

    return AnalystCredentials(
        analyst_name=name,
        role_name=role_name,
        r2_account_id=context.r2_account_id,
        metrics=LakeCredentials(
            catalog_dsn=SecretStr(
                build_reader_dsn(
                    context.metrics_catalog_owner_dsn.get_secret_value(), role=role_name, password=password
                )
            ),
            r2_bucket=context.metrics_bucket,
            r2_access_key_id=metrics_key_id,
            r2_secret_access_key=metrics_secret,
        ),
        transcripts=transcripts,
    )


def revoke_analyst_access(context: AnalyticsAnalystAdminContext, name: AnalystName) -> AnalystRemovalReport:
    """Remove one analyst's role, grants, and tokens (idempotent; absent pieces are skipped)."""
    role_name = analyst_role_name(name)
    with log_span("Revoking the {} role's grants on both catalogs", role_name):
        _revoke_readonly_grants(context.metrics_catalog_owner_dsn, role_name)
        _revoke_readonly_grants(context.transcripts_catalog_owner_dsn, role_name)
    is_role_dropped = _drop_role_if_exists(context.metrics_catalog_owner_dsn, role_name)

    cloudflare = _cloudflare_credentials(context)
    revoked_token_count = 0
    for lake in AnalyticsLake:
        with log_span("Revoking the {} {} token(s)", str(name), lake.value):
            revoked_token_count += _delete_tokens_named(cloudflare, analyst_token_name(name, lake))
    return AnalystRemovalReport(is_role_dropped=is_role_dropped, revoked_token_count=revoked_token_count)


def _analyst_role_names(owner_dsn: SecretStr) -> list[str]:
    with _catalog_cursor(owner_dsn, "Could not list analyst roles on the catalog database") as cursor:
        cursor.execute("SELECT rolname FROM pg_roles WHERE rolname LIKE %s ORDER BY rolname", ("analyst\\_%",))
        return [str(row[0]) for row in cursor.fetchall()]


def _grantees_with_table_grants(owner_dsn: SecretStr) -> frozenset[str]:
    """Roles holding explicit table grants in the connected database's public schema."""
    with _catalog_cursor(owner_dsn, "Could not list analyst table grants on the catalog database") as cursor:
        cursor.execute(
            "SELECT DISTINCT grantee FROM information_schema.role_table_grants"
            " WHERE table_schema = 'public' AND grantee LIKE %s",
            ("analyst\\_%",),
        )
        return frozenset(str(row[0]) for row in cursor.fetchall())


def list_analyst_access(context: AnalyticsAnalystAdminContext) -> list[AnalystListing]:
    """Reconstruct every analyst's access state from the roles, grants, and tokens."""
    role_names = _analyst_role_names(context.metrics_catalog_owner_dsn)
    metrics_grantees = _grantees_with_table_grants(context.metrics_catalog_owner_dsn)
    transcripts_grantees = _grantees_with_table_grants(context.transcripts_catalog_owner_dsn)

    token_count_by_name_and_lake: dict[tuple[str, AnalyticsLake], int] = {}
    for token in list_cloudflare_account_tokens(_cloudflare_credentials(context)):
        parsed = analyst_name_and_lake_from_token_name(token.name)
        if parsed is not None:
            token_count_by_name_and_lake[parsed] = token_count_by_name_and_lake.get(parsed, 0) + 1

    # Union of role-derived and token-derived names, so a half-removed analyst
    # still shows up rather than silently leaking a token.
    analyst_names: set[str] = set()
    role_name_by_analyst: dict[str, str] = {}
    for role_name in role_names:
        parsed_name = analyst_name_from_role_name(role_name)
        if parsed_name is not None:
            analyst_names.add(parsed_name)
            role_name_by_analyst[parsed_name] = role_name
    analyst_names.update(parsed_name for parsed_name, _lake in token_count_by_name_and_lake)

    listings: list[AnalystListing] = []
    for analyst in sorted(analyst_names):
        role_name = role_name_by_analyst.get(analyst, "")
        listings.append(
            AnalystListing(
                analyst_name=analyst,
                is_role_present=bool(role_name),
                is_metrics_granted=role_name in metrics_grantees,
                is_transcripts_granted=role_name in transcripts_grantees,
                metrics_token_count=token_count_by_name_and_lake.get((analyst, AnalyticsLake.METRICS), 0),
                transcripts_token_count=token_count_by_name_and_lake.get((analyst, AnalyticsLake.TRANSCRIPTS), 0),
            )
        )
    return listings


@pure
def _toml_string(value: str) -> str:
    # json.dumps escapes exactly `"`, `\`, and chars < 0x20, all with escapes
    # TOML basic strings also accept; ensure_ascii=False keeps astral-plane
    # chars literal (TOML forbids the surrogate-pair \uXXXX escapes the
    # default ASCII mode would emit for them), leaving U+007F as the one char
    # JSON may emit raw but TOML requires escaped.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


@pure
def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@pure
def _attach_snippet_lines(lake_label: str, lake: LakeCredentials, r2_account_id: str) -> list[str]:
    return [
        f"#   CREATE SECRET {lake_label}_bucket (",
        "#       TYPE r2,",
        f"#       KEY_ID {_sql_literal(lake.r2_access_key_id)},",
        f"#       SECRET {_sql_literal(lake.r2_secret_access_key.get_secret_value())},",
        f"#       ACCOUNT_ID {_sql_literal(r2_account_id)},",
        f"#       SCOPE {_sql_literal('r2://' + lake.r2_bucket)}",
        "#   );",
        f"#   ATTACH {_sql_literal('ducklake:postgres:' + lake.catalog_dsn.get_secret_value())} "
        f"AS {lake_label} (READ_ONLY);",
    ]


@pure
def _lake_section_lines(section_name: str, lake: LakeCredentials) -> list[str]:
    return [
        f"[{section_name}]",
        f"catalog_dsn = {_toml_string(lake.catalog_dsn.get_secret_value())}",
        f"r2_bucket = {_toml_string(lake.r2_bucket)}",
        f"r2_access_key_id = {_toml_string(lake.r2_access_key_id)}",
        f"r2_secret_access_key = {_toml_string(lake.r2_secret_access_key.get_secret_value())}",
    ]


@pure
def render_analyst_credentials_toml(credentials: AnalystCredentials, env_name: str, minted_at: datetime) -> str:
    """The self-documenting hand-off file for one analyst (secrets included -- deliver privately).

    The header comment carries a copy-pasteable DuckDB session with the real
    values already substituted, so the analyst (or their coding agent) can go
    from this file to query results with zero other context; the TOML body
    carries the same values structured for tooling.
    """
    lines = [
        f"# Imbue minds analytics ({env_name}) -- read-only analyst credentials for {credentials.analyst_name!r}.",
        f"# Minted {minted_at:%Y-%m-%d %H:%M} UTC by `minds-admin analytics analyst add`; re-running that",
        "# command rotates these credentials, and `... analyst remove` revokes them.",
        "# SECRETS: deliver privately and store like a password.",
        "#",
        "# What this grants: read-only SQL over the minds analytics lakes, queried with",
        "# DuckDB from your own machine (there is no dashboard service). Tables,",
        "# worked-example queries, data start dates, and gotchas are documented in",
        "# apps/analytics/reports/README.md in the mngr repo -- start there.",
        "#",
        "# Quick start (`uv run --with duckdb --with pytz python`, or the duckdb CLI):",
        "#",
        "#   INSTALL ducklake; LOAD ducklake;",
        "#   INSTALL postgres; LOAD postgres;",
        "#   INSTALL httpfs; LOAD httpfs;",
        *_attach_snippet_lines("metrics", credentials.metrics, credentials.r2_account_id),
    ]
    if credentials.transcripts is not None:
        lines.extend(_attach_snippet_lines("transcripts", credentials.transcripts, credentials.r2_account_id))
    lines.extend(
        [
            "#   SELECT * FROM metrics.gold.activity LIMIT 10;",
            "",
            f"analyst = {_toml_string(str(credentials.analyst_name))}",
            f"environment = {_toml_string(env_name)}",
            f"r2_account_id = {_toml_string(credentials.r2_account_id)}",
            "",
            *_lake_section_lines("metrics", credentials.metrics),
        ]
    )
    if credentials.transcripts is not None:
        lines.extend(["", *_lake_section_lines("transcripts", credentials.transcripts)])
    return "\n".join(lines) + "\n"
