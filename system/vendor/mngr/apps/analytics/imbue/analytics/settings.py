"""Environment-derived configuration for the analytics app.

Every value arrives via the ``analytics-<tier>-<deploy_id>`` Modal Secret
(pushed by ``minds-admin env deploy`` from the tier's Vault entry; schema in
``.minds/template/analytics.sh``). Loaded per-run rather than at import so a
misconfigured deploy fails inside the cron with a clear error instead of
crashing the container at import time.
"""

import os
from typing import Final

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import ValidationError

from imbue.analytics.errors import AnalyticsConfigError

# Default glob (within the OpenObserve bucket) matching the tier's log parquet
# files. OpenObserve's object layout is internal to it -- re-verify on any
# OpenObserve version bump (the instance follows a replace-not-upgrade
# lifecycle) and override via ANALYTICS_LOGS_PARQUET_GLOB when it moves.
_DEFAULT_LOGS_PARQUET_GLOB: Final[str] = "files/default/logs/**/*.parquet"

_DEFAULT_AGGREGATION_WINDOW_DAYS: Final[int] = 7

# Snapshot expiry for the lakes: the undelete window and the bound on physical
# deletion after a DELETE. Deliberately uniform across lakes (see
# specs/minds-analytics/spec.md, "Retention and deletion").
SNAPSHOT_RETENTION_DAYS: Final[int] = 30

# Collection-loop tuning defaults, each overridable per tier via the analytics
# Modal secret so testing tiers can run the loop hot without a code change.
_DEFAULT_COLLECTION_INTERVAL_SECONDS: Final[int] = 3600
_DEFAULT_COLLECTION_PARALLELISM: Final[int] = 4
_DEFAULT_COLLECTION_WORKSPACE_TIMEOUT_SECONDS: Final[int] = 600
_DEFAULT_COLLECTION_RUN_BUDGET_BYTES: Final[int] = 256 * 1024 * 1024


class AnalyticsSettings(BaseModel):
    """The analytics app's runtime configuration, read from the environment."""

    model_config = ConfigDict(frozen=True)

    metrics_catalog_dsn: SecretStr = Field(description="Postgres DSN of the metrics DuckLake catalog database")
    transcripts_catalog_dsn: SecretStr = Field(description="Postgres DSN of the transcripts DuckLake catalog database")
    ops_dsn: SecretStr = Field(description="Postgres DSN of the ops database (job bookkeeping)")
    rsc_readonly_dsn: SecretStr = Field(description="Read-only Postgres DSN on the connector's product database")
    metrics_bucket: str = Field(description="R2 bucket holding the metrics lake's parquet data")
    metrics_r2_access_key_id: str = Field(description="S3 access key id scoped to the metrics bucket (readwrite)")
    metrics_r2_secret_access_key: SecretStr = Field(description="S3 secret for the metrics bucket key")
    transcripts_bucket: str = Field(description="R2 bucket holding the transcripts lake's parquet data")
    transcripts_r2_access_key_id: str = Field(
        description="S3 access key id scoped to the transcripts bucket (readwrite)"
    )
    transcripts_r2_secret_access_key: SecretStr = Field(description="S3 secret for the transcripts bucket key")
    logs_bucket: str = Field(description="The tier's OpenObserve R2 bucket (read-only source)")
    logs_r2_access_key_id: str = Field(description="S3 access key id scoped to the OpenObserve bucket (read-only)")
    logs_r2_secret_access_key: SecretStr = Field(description="S3 secret for the OpenObserve bucket key")
    r2_account_id: str = Field(description="Cloudflare account id both buckets live under")
    logs_parquet_glob: str = Field(description="Glob (within the logs bucket) matching the tier's log parquet files")
    logs_env_filter: str = Field(
        description=(
            "When non-empty, the log views include only lines stamped with this minds_env value. "
            "Set by per-env (dev) analytics stacks, whose OpenObserve bucket is shared tier-wide; "
            "blank on shared tiers includes every line."
        )
    )
    aggregation_window_days: int = Field(description="Trailing window (days) the activity aggregation recomputes")


class CollectionSettings(BaseModel):
    """The collection loop's tuning knobs plus the pool SSH key it hops with."""

    model_config = ConfigDict(frozen=True)

    pool_ssh_private_key: SecretStr = Field(description="Ed25519 PEM the pool uses to reach workspace sshds")
    interval_seconds: int = Field(gt=0, description="Minimum seconds between collection attempts on one workspace")
    parallelism: int = Field(gt=0, description="Workspaces collected concurrently by one poll run")
    # Must stay positive: GNU timeout treats 0 as "no timeout", which would
    # silently remove the per-workspace bound on the remote run.
    workspace_timeout_seconds: int = Field(
        gt=0, description="Hard bound on one workspace's collection (incl. injection)"
    )
    run_budget_bytes: int = Field(gt=0, description="Per-workspace pre-redaction input budget handed to the script")


def _require_env(key: str) -> str:
    value = os.environ.get(key, "")
    if not value:
        raise AnalyticsConfigError(f"Missing required environment variable: {key}")
    return value


def _read_optional_int_env(key: str, default: int) -> int:
    # An empty value means "declared but unset" per the secret template, so it
    # falls back to the default just like an absent variable.
    raw_value = os.environ.get(key, "")
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        raise AnalyticsConfigError(f"{key} must be an integer, got: {raw_value!r}") from None


def _read_aggregation_window_days() -> int:
    return _read_optional_int_env("ANALYTICS_AGGREGATION_WINDOW_DAYS", _DEFAULT_AGGREGATION_WINDOW_DAYS)


def load_analytics_settings() -> AnalyticsSettings:
    """Raises AnalyticsConfigError when a required environment variable is absent or a value is malformed."""
    return AnalyticsSettings(
        metrics_catalog_dsn=SecretStr(_require_env("ANALYTICS_METRICS_CATALOG_URL")),
        transcripts_catalog_dsn=SecretStr(_require_env("ANALYTICS_TRANSCRIPTS_CATALOG_URL")),
        ops_dsn=SecretStr(_require_env("ANALYTICS_OPS_DATABASE_URL")),
        rsc_readonly_dsn=SecretStr(_require_env("ANALYTICS_RSC_READONLY_DATABASE_URL")),
        metrics_bucket=_require_env("ANALYTICS_METRICS_R2_BUCKET"),
        metrics_r2_access_key_id=_require_env("ANALYTICS_METRICS_R2_ACCESS_KEY_ID"),
        metrics_r2_secret_access_key=SecretStr(_require_env("ANALYTICS_METRICS_R2_SECRET_ACCESS_KEY")),
        transcripts_bucket=_require_env("ANALYTICS_TRANSCRIPTS_R2_BUCKET"),
        transcripts_r2_access_key_id=_require_env("ANALYTICS_TRANSCRIPTS_R2_ACCESS_KEY_ID"),
        transcripts_r2_secret_access_key=SecretStr(_require_env("ANALYTICS_TRANSCRIPTS_R2_SECRET_ACCESS_KEY")),
        logs_bucket=_require_env("ANALYTICS_LOGS_R2_BUCKET"),
        logs_r2_access_key_id=_require_env("ANALYTICS_LOGS_R2_ACCESS_KEY_ID"),
        logs_r2_secret_access_key=SecretStr(_require_env("ANALYTICS_LOGS_R2_SECRET_ACCESS_KEY")),
        r2_account_id=_require_env("ANALYTICS_R2_ACCOUNT_ID"),
        logs_parquet_glob=os.environ.get("ANALYTICS_LOGS_PARQUET_GLOB", _DEFAULT_LOGS_PARQUET_GLOB),
        logs_env_filter=os.environ.get("ANALYTICS_LOGS_ENV_FILTER", ""),
        aggregation_window_days=_read_aggregation_window_days(),
    )


def load_collection_settings() -> CollectionSettings:
    """Raises AnalyticsConfigError when the pool key is absent or a tuning value is malformed.

    The pool SSH key arrives via the ``pool-ssh`` Modal Secret the collection
    function additionally attaches (the same key the connector leases with);
    the tuning knobs live in the analytics secret, default sensibly, and must
    be positive (the model rejects zero/negative overrides loudly rather than
    silently correcting them).
    """
    try:
        return CollectionSettings(
            pool_ssh_private_key=SecretStr(_require_env("POOL_SSH_PRIVATE_KEY")),
            interval_seconds=_read_optional_int_env(
                "ANALYTICS_COLLECTION_INTERVAL_SECONDS", _DEFAULT_COLLECTION_INTERVAL_SECONDS
            ),
            parallelism=_read_optional_int_env("ANALYTICS_COLLECTION_PARALLELISM", _DEFAULT_COLLECTION_PARALLELISM),
            workspace_timeout_seconds=_read_optional_int_env(
                "ANALYTICS_COLLECTION_WORKSPACE_TIMEOUT_SECONDS", _DEFAULT_COLLECTION_WORKSPACE_TIMEOUT_SECONDS
            ),
            run_budget_bytes=_read_optional_int_env(
                "ANALYTICS_COLLECTION_RUN_BUDGET_BYTES", _DEFAULT_COLLECTION_RUN_BUDGET_BYTES
            ),
        )
    except ValidationError as e:
        raise AnalyticsConfigError(f"Invalid collection settings: {e}") from e
