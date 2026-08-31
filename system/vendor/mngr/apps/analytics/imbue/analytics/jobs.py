"""The cron job bodies: session assembly, run bookkeeping, and duration warnings.

The Modal entrypoint (app.py) only schedules these; everything testable lives
here. Neither job ever logs payload content -- container logs flow to the
ops telemetry store, so log lines carry only counts, durations, and error
summaries.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final
from typing import TypeVar

import duckdb
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from tenacity import before_sleep_log
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_fixed

import imbue.analytics.ops_db as ops_db
from imbue.analytics.aggregation import run_aggregation
from imbue.analytics.collection import run_collection_poll
from imbue.analytics.errors import AnalyticsError
from imbue.analytics.lake import RAW_TABLE_DDL_STATEMENTS
from imbue.analytics.lake import TRANSCRIPTS_RAW_TABLE_DDL_STATEMENTS
from imbue.analytics.lake import attach_ducklake
from imbue.analytics.lake import attach_postgres_readonly
from imbue.analytics.lake import create_duckdb_session
from imbue.analytics.lake import create_r2_secret
from imbue.analytics.lake import ensure_raw_tables
from imbue.analytics.lake import install_session_extensions
from imbue.analytics.lake import run_maintenance
from imbue.analytics.log_views import DEFAULT_BODY_COLUMN
from imbue.analytics.log_views import DEFAULT_TIMESTAMP_COLUMN
from imbue.analytics.log_views import create_log_views
from imbue.analytics.settings import AnalyticsSettings
from imbue.analytics.settings import SNAPSHOT_RETENTION_DAYS
from imbue.analytics.settings import load_analytics_settings
from imbue.analytics.settings import load_collection_settings

logger = logging.getLogger(__name__)

AGGREGATION_JOB_NAME: Final[str] = "aggregation"
LAKE_MAINTENANCE_JOB_NAME: Final[str] = "lake_maintenance"
COLLECTION_POLL_JOB_NAME: Final[str] = "collection_poll"

# Two-threshold timing: the Modal function timeout is the hard bound; crossing
# the warning threshold logs loudly (and shows in pipeline_health's duration
# column) so we notice a job approaching its budget before it starts failing.
AGGREGATION_WARN_SECONDS: Final[float] = 300.0
LAKE_MAINTENANCE_WARN_SECONDS: Final[float] = 600.0
COLLECTION_POLL_WARN_SECONDS: Final[float] = 600.0

# One in-cron retry for reads racing OpenObserve's parquet compaction: the log
# glob is listed and read at different moments, so an object can 404 between
# the two. A second attempt re-lists; anything persistent still fails the run.
_TRANSIENT_SOURCE_RETRY_ATTEMPTS: Final[int] = 2
_TRANSIENT_SOURCE_RETRY_WAIT_SECONDS: Final[float] = 15.0

_CallableT = TypeVar("_CallableT", bound=Callable[..., Any])


def _is_transient_source_error(exception: BaseException) -> bool:
    """Whether an error chain bottoms out in an object-store HTTP read failure."""
    cause: BaseException | None = exception
    while cause is not None:
        if isinstance(cause, duckdb.HTTPException):
            return True
        cause = cause.__cause__
    return False


def build_transient_source_retry(wait_seconds: float) -> Callable[[_CallableT], _CallableT]:
    return retry(
        retry=retry_if_exception(_is_transient_source_error),
        stop=stop_after_attempt(_TRANSIENT_SOURCE_RETRY_ATTEMPTS),
        wait=wait_fixed(wait_seconds),
        # A retried attempt never reaches job_runs (the retry is inside the
        # recorded body), so the warning here is its only trace.
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


class JobRunRecord(BaseModel):
    """One job execution's bookkeeping row (the source for pipeline_health)."""

    model_config = ConfigDict(frozen=True)

    job_name: str = Field(description="Which cron ran")
    started_at: datetime = Field(description="UTC start of the run")
    finished_at: datetime = Field(description="UTC end of the run")
    is_success: bool = Field(description="Whether the run completed without error")
    detail: str = Field(description="Error summary for failed runs; empty on success")


def run_recorded_job(
    job_name: str,
    warn_seconds: float,
    job_body: Callable[[], dict[str, int]],
    record_run: Callable[[JobRunRecord], None],
) -> dict[str, int]:
    """Run one job body with job_runs bookkeeping and the duration warning.

    The run row is written for success and failure alike (the session and
    lake helpers wrap every external failure into an AnalyticsError);
    failures then re-raise so the scheduler marks the cron run failed.
    """
    started_at = datetime.now(timezone.utc)
    start_monotonic = time.monotonic()
    try:
        counters = job_body()
    except AnalyticsError as e:
        record_run(
            JobRunRecord(
                job_name=job_name,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                is_success=False,
                detail=str(e)[:500],
            )
        )
        raise
    duration_seconds = time.monotonic() - start_monotonic
    record_run(
        JobRunRecord(
            job_name=job_name,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            is_success=True,
            detail="",
        )
    )
    if duration_seconds > warn_seconds:
        logger.warning(
            "Job %s took %.1fs (warning threshold %.0fs) -- approaching its time budget",
            job_name,
            duration_seconds,
            warn_seconds,
        )
    logger.info("Job %s finished in %.1fs: %s", job_name, duration_seconds, counters)
    return counters


def _register_metrics_lake(connection: Any, settings: AnalyticsSettings) -> None:
    """Register the metrics bucket's R2 secret and attach the metrics DuckLake as ``metrics``."""
    create_r2_secret(
        connection,
        secret_name="metrics_bucket_secret",
        key_id=settings.metrics_r2_access_key_id,
        secret=settings.metrics_r2_secret_access_key.get_secret_value(),
        account_id=settings.r2_account_id,
        bucket=settings.metrics_bucket,
    )
    attach_ducklake(
        connection,
        alias="metrics",
        catalog_dsn=settings.metrics_catalog_dsn.get_secret_value(),
        data_path=f"r2://{settings.metrics_bucket}/lake/",
    )


def _register_transcripts_lake(connection: Any, settings: AnalyticsSettings) -> None:
    """Register the transcripts bucket's R2 secret and attach the transcripts DuckLake."""
    create_r2_secret(
        connection,
        secret_name="transcripts_bucket_secret",
        key_id=settings.transcripts_r2_access_key_id,
        secret=settings.transcripts_r2_secret_access_key.get_secret_value(),
        account_id=settings.r2_account_id,
        bucket=settings.transcripts_bucket,
    )
    attach_ducklake(
        connection,
        alias="transcripts",
        catalog_dsn=settings.transcripts_catalog_dsn.get_secret_value(),
        data_path=f"r2://{settings.transcripts_bucket}/lake/",
    )


def _build_dual_lake_session(settings: AnalyticsSettings) -> Any:
    """A DuckDB session with both DuckLakes attached and the raw landing tables ensured."""
    connection = create_duckdb_session()
    install_session_extensions(connection)
    _register_metrics_lake(connection, settings)
    _register_transcripts_lake(connection, settings)
    ensure_raw_tables(connection, RAW_TABLE_DDL_STATEMENTS)
    ensure_raw_tables(connection, TRANSCRIPTS_RAW_TABLE_DDL_STATEMENTS)
    return connection


def build_metrics_session(settings: AnalyticsSettings) -> Any:
    """One DuckDB session with the production aliases: metrics, transcripts, rsc, ops, logs."""
    connection = _build_dual_lake_session(settings)
    create_r2_secret(
        connection,
        secret_name="logs_bucket_secret",
        key_id=settings.logs_r2_access_key_id,
        secret=settings.logs_r2_secret_access_key.get_secret_value(),
        account_id=settings.r2_account_id,
        bucket=settings.logs_bucket,
    )
    attach_postgres_readonly(connection, alias="rsc", dsn=settings.rsc_readonly_dsn.get_secret_value())
    attach_postgres_readonly(connection, alias="ops", dsn=settings.ops_dsn.get_secret_value())
    create_log_views(
        connection,
        parquet_glob=f"r2://{settings.logs_bucket}/{settings.logs_parquet_glob}",
        body_column=DEFAULT_BODY_COLUMN,
        timestamp_column=DEFAULT_TIMESTAMP_COLUMN,
        env_filter=settings.logs_env_filter,
    )
    return connection


def record_run_row_in_ops_db(connection_factory: Callable[[], Any], record: JobRunRecord) -> None:
    """Append one run's bookkeeping row via a fresh ops-DB connection."""
    connection = connection_factory()
    try:
        ops_db.record_job_run(
            connection,
            job_name=record.job_name,
            started_at=record.started_at,
            finished_at=record.finished_at,
            is_success=record.is_success,
            detail=record.detail,
        )
    finally:
        connection.close()


@build_transient_source_retry(_TRANSIENT_SOURCE_RETRY_WAIT_SECONDS)
def _aggregation_body(settings: AnalyticsSettings) -> dict[str, int]:
    connection = build_metrics_session(settings)
    try:
        window_start = (datetime.now(timezone.utc) - timedelta(days=settings.aggregation_window_days)).date()
        counters = run_aggregation(connection, window_start)
    finally:
        connection.close()
    return {
        "activity_rows": counters.activity_rows,
        "client_version_rows": counters.client_version_rows,
        "account_rows": counters.account_rows,
        "funnel_rows": counters.funnel_rows,
        "pipeline_health_rows": counters.pipeline_health_rows,
        "transcript_daily_rows": counters.transcript_daily_rows,
        "collection_health_rows": counters.collection_health_rows,
    }


def _maintenance_body(settings: AnalyticsSettings) -> dict[str, int]:
    connection = _build_dual_lake_session(settings)
    try:
        # Snapshot expiry on the transcripts lake is what physically removes
        # deleted transcript rows within the 30-day bound (see deletion.py).
        run_maintenance(connection, catalog_alias="metrics", snapshot_retention_days=SNAPSHOT_RETENTION_DAYS)
        run_maintenance(connection, catalog_alias="transcripts", snapshot_retention_days=SNAPSHOT_RETENTION_DAYS)
    finally:
        connection.close()
    return {"maintained_lakes": 2}


def run_aggregation_job() -> dict[str, int]:
    settings = load_analytics_settings()
    return run_recorded_job(
        AGGREGATION_JOB_NAME,
        AGGREGATION_WARN_SECONDS,
        job_body=lambda: _aggregation_body(settings),
        record_run=lambda record: record_run_row_in_ops_db(
            lambda: ops_db.get_ops_db_connection(settings.ops_dsn.get_secret_value()), record
        ),
    )


def run_lake_maintenance_job() -> dict[str, int]:
    settings = load_analytics_settings()
    return run_recorded_job(
        LAKE_MAINTENANCE_JOB_NAME,
        LAKE_MAINTENANCE_WARN_SECONDS,
        job_body=lambda: _maintenance_body(settings),
        record_run=lambda record: record_run_row_in_ops_db(
            lambda: ops_db.get_ops_db_connection(settings.ops_dsn.get_secret_value()), record
        ),
    )


def _collection_body(settings: AnalyticsSettings) -> dict[str, int]:
    collection_settings = load_collection_settings()
    lake_connection = _build_dual_lake_session(settings)
    try:
        counters = run_collection_poll(
            settings=settings,
            collection_settings=collection_settings,
            lake_connection=lake_connection,
        )
    finally:
        lake_connection.close()
    return counters


def run_collection_poll_job() -> dict[str, int]:
    settings = load_analytics_settings()
    return run_recorded_job(
        COLLECTION_POLL_JOB_NAME,
        COLLECTION_POLL_WARN_SECONDS,
        job_body=lambda: _collection_body(settings),
        record_run=lambda record: record_run_row_in_ops_db(
            lambda: ops_db.get_ops_db_connection(settings.ops_dsn.get_secret_value()), record
        ),
    )
