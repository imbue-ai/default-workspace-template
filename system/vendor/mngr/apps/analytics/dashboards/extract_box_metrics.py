"""Extract a dev env's bare-metal box telemetry into a local DuckDB file for the Evidence dashboards.

The tier's OpenObserve instance lands every OTLP metric as parquet in the
shared observability R2 bucket (one directory per metric stream). This script
reads the hostmetrics streams for the boxes registered to ONE dev env (the
``bare_metal_servers`` table in that env's own connector database identifies
them; the shared dev bucket also carries other envs' boxes, relays, and Modal
metrics), pre-aggregates them into small chart-ready tables, and writes them
to ``data/box_metrics.duckdb`` for ``evidence sources`` to consume.

Credentials come from the env's local deploy state
(``~/.minds-<env>/secrets.toml``, written by ``minds-admin env deploy``): the
read-only key on the observability bucket plus the analytics_reader DSN on the
env's connector database. Run from the repo root:

    uv run python apps/analytics/dashboards/extract_box_metrics.py --env dev-josh-1
"""

import tomllib
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import click
import duckdb
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.analytics.errors import AnalyticsError
from imbue.analytics.lake import attach_postgres_readonly
from imbue.analytics.lake import create_r2_secret
from imbue.analytics.lake import install_session_extensions
from imbue.analytics.lake import quote_sql_literal
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.pure import pure

_DEFAULT_ENV_NAME: Final[str] = "dev-josh-1"
_DEFAULT_WINDOW_DAYS: Final[int] = 7
_DEFAULT_OUTPUT_RELATIVE_PATH: Final[str] = "data/box_metrics.duckdb"

# OpenObserve's parquet layout inside the bucket: one directory per metric
# stream, partitioned by year/month/day/hour. Internal to OpenObserve --
# re-verify on version bumps (same caveat as the analytics app's log views).
_METRICS_PREFIX: Final[str] = "files/default/metrics"

# The hostmetrics streams the dashboard reads. Each entry becomes one raw
# DuckDB view over the stream's parquet, filtered to the env's boxes.
_STREAM_NAMES: Final[tuple[str, ...]] = (
    "system_cpu_load_average_1m",
    "system_cpu_load_average_5m",
    "system_cpu_load_average_15m",
    "system_cpu_time",
    "system_memory_usage",
    "system_filesystem_usage",
    "system_network_io",
    "process_memory_usage",
)

# Keys the env's secrets.toml must carry (persisted there by the per-env
# analytics stack provisioning; see minds_admin's analytics_stack.py).
_REQUIRED_SECRET_KEYS: Final[tuple[str, ...]] = (
    "ANALYTICS_LOGS_R2_BUCKET",
    "ANALYTICS_LOGS_R2_ACCESS_KEY_ID",
    "ANALYTICS_LOGS_R2_SECRET_ACCESS_KEY",
    "ANALYTICS_R2_ACCOUNT_ID",
    "ANALYTICS_RSC_READONLY_DATABASE_URL",
)


class DashboardExtractError(AnalyticsError, RuntimeError):
    """Raised when the box-metrics extract cannot complete."""


class ExtractArguments(FrozenModel):
    """Parsed command line arguments for the box-metrics extract."""

    env_name: str = Field(description="Dev env whose boxes to extract (reads ~/.minds-<env>/secrets.toml)")
    window_days: int = Field(description="Trailing window of telemetry to extract, in days", ge=1)
    output_path: Path = Field(description="DuckDB file to (re)write; consumed by the Evidence 'boxes' source")


class EnvAnalyticsCredentials(FrozenModel):
    """The subset of the env's local deploy secrets the extract needs."""

    logs_bucket: str = Field(description="The tier's shared OpenObserve R2 bucket")
    logs_access_key_id: str = Field(description="S3 access key id scoped read-only to the OpenObserve bucket")
    logs_secret_access_key: SecretStr = Field(description="S3 secret for the OpenObserve bucket key")
    r2_account_id: str = Field(description="Cloudflare account id the bucket lives under")
    rsc_readonly_dsn: SecretStr = Field(description="analytics_reader DSN on the env's connector database")


def load_env_analytics_credentials(env_name: str) -> EnvAnalyticsCredentials:
    """Raises DashboardExtractError when the env's secrets.toml is absent or missing analytics keys."""
    secrets_path = Path.home() / f".minds-{env_name}" / "secrets.toml"
    if not secrets_path.is_file():
        raise DashboardExtractError(
            f"No local deploy state at {secrets_path}; deploy the env with analytics first "
            f"(minds-admin env deploy --with-analytics)"
        )
    raw_secrets = tomllib.loads(secrets_path.read_text()).get("secrets", {})
    missing_keys = [key for key in _REQUIRED_SECRET_KEYS if not raw_secrets.get(key)]
    if missing_keys:
        raise DashboardExtractError(
            f"{secrets_path} is missing analytics keys {missing_keys}; re-deploy the env with "
            f"--with-analytics to provision them"
        )
    return EnvAnalyticsCredentials(
        logs_bucket=raw_secrets["ANALYTICS_LOGS_R2_BUCKET"],
        logs_access_key_id=raw_secrets["ANALYTICS_LOGS_R2_ACCESS_KEY_ID"],
        logs_secret_access_key=SecretStr(raw_secrets["ANALYTICS_LOGS_R2_SECRET_ACCESS_KEY"]),
        r2_account_id=raw_secrets["ANALYTICS_R2_ACCOUNT_ID"],
        rsc_readonly_dsn=SecretStr(raw_secrets["ANALYTICS_RSC_READONLY_DATABASE_URL"]),
    )


@pure
def month_starts_within(window_start: datetime, window_end: datetime) -> list[datetime]:
    """The first day of every calendar month the window touches, oldest first."""
    month_starts: list[datetime] = []
    cursor = window_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= window_end:
        month_starts.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return month_starts


@pure
def stream_month_globs(bucket: str, stream_name: str, month_starts: list[datetime]) -> list[str]:
    return [
        f"r2://{bucket}/{_METRICS_PREFIX}/{stream_name}/{month_start.year}/{month_start.month:02d}/**/*.parquet"
        for month_start in month_starts
    ]


def _matching_globs(connection: Any, globs: list[str]) -> list[str]:
    """The subset of globs that match at least one object (read_parquet errors on empty globs)."""
    matching: list[str] = []
    for glob_pattern in globs:
        try:
            row = connection.execute(f"SELECT count(*) FROM glob({quote_sql_literal(glob_pattern)})").fetchone()
        except duckdb.Error as e:
            raise DashboardExtractError(f"Cannot list parquet objects matching {glob_pattern!r}") from e
        if row is not None and row[0] > 0:
            matching.append(glob_pattern)
    return matching


def _create_stream_view(
    connection: Any,
    credentials: EnvAnalyticsCredentials,
    stream_name: str,
    month_starts: list[datetime],
    window_start: datetime,
) -> None:
    """One raw view per stream: parquet rows for the env's boxes within the window.

    The box filter joins the ``servers`` table (already materialized from the
    env's connector database), so the shared dev-tier bucket's other senders
    (other envs' boxes, relays, Modal) never leak in.
    """
    globs = stream_month_globs(credentials.logs_bucket, stream_name, month_starts)
    matching_globs = _matching_globs(connection, globs)
    if not matching_globs:
        raise DashboardExtractError(
            f"No parquet objects found for metric stream {stream_name!r} in the window; "
            f"has the collector been running on the env's boxes?"
        )
    glob_list_sql = ", ".join(quote_sql_literal(glob_pattern) for glob_pattern in matching_globs)
    window_start_micros = int(window_start.timestamp() * 1_000_000)
    try:
        connection.execute(
            f"CREATE OR REPLACE VIEW raw_{stream_name} AS "
            f"SELECT to_timestamp(_timestamp / 1000000) AS observed_at, * "
            f"FROM read_parquet([{glob_list_sql}], union_by_name=true) "
            f"WHERE _timestamp >= {window_start_micros} "
            f"AND host_name IN (SELECT host_name FROM servers)"
        )
    except duckdb.Error as e:
        raise DashboardExtractError(f"Cannot create the raw view for metric stream {stream_name!r}") from e


def _materialize_servers_table(connection: Any) -> int:
    """The env's bare-metal servers with their slice occupancy, from the attached connector database.

    Returns the server count. The metrics join key is the short hostname (the
    first label of the OVH service name), which is what the collector's
    resourcedetection stamps as host_name.
    """
    try:
        connection.execute(
            "CREATE OR REPLACE TABLE servers AS "
            "SELECT split_part(server.ovh_service_name, '.', 1) AS host_name,"
            " server.ovh_service_name,"
            " server.plan_code,"
            " server.region,"
            " server.public_address,"
            " server.cpu_cores,"
            " server.cpu_threads,"
            " server.ram_gb,"
            " server.disk_gb,"
            " server.slot_count,"
            " server.status,"
            " count(slice.id) AS slice_count,"
            " count(slice.id) FILTER (WHERE slice.leased_to_user IS NOT NULL AND slice.released_at IS NULL)"
            "  AS leased_slice_count "
            "FROM rsc.public.bare_metal_servers AS server "
            "LEFT JOIN rsc.public.pool_hosts AS slice ON slice.bare_metal_server_id = server.id "
            "WHERE server.ovh_service_name IS NOT NULL "
            "GROUP BY ALL"
        )
    except duckdb.Error as e:
        raise DashboardExtractError("Cannot materialize the servers table from the connector database") from e
    row = connection.execute("SELECT count(*) FROM servers").fetchone()
    return int(row[0]) if row is not None else 0


# Chart-ready tables, each a single statement over the raw stream views. All
# bucketing happens here so the Evidence pages stay simple selects.
_TABLE_STATEMENTS: Final[tuple[tuple[str, str], ...]] = (
    (
        "load_average",
        "CREATE OR REPLACE TABLE load_average AS "
        "WITH unioned AS ("
        " SELECT host_name, observed_at, '1m' AS window_label, value FROM raw_system_cpu_load_average_1m"
        " UNION ALL"
        " SELECT host_name, observed_at, '5m' AS window_label, value FROM raw_system_cpu_load_average_5m"
        " UNION ALL"
        " SELECT host_name, observed_at, '15m' AS window_label, value FROM raw_system_cpu_load_average_15m"
        ") "
        "SELECT host_name, time_bucket(INTERVAL 5 MINUTE, observed_at) AS bucket_at,"
        " avg(value) FILTER (WHERE window_label = '1m') AS load_1m,"
        " avg(value) FILTER (WHERE window_label = '5m') AS load_5m,"
        " avg(value) FILTER (WHERE window_label = '15m') AS load_15m "
        "FROM unioned GROUP BY ALL",
    ),
    (
        "cpu_utilization",
        # system_cpu_time lands as ONE cumulative CPU-seconds counter per
        # (host, state), already summed across cores (verified against the
        # live parquet: a box's total advance rate equals its logical CPU
        # count, and no per-core attribute column exists). Utilization is the
        # per-bucket delta share, which is scale-invariant either way; counter
        # resets (reboots) produce negative deltas and are dropped.
        "CREATE OR REPLACE TABLE cpu_utilization AS "
        "WITH sampled AS ("
        " SELECT host_name, state, time_bucket(INTERVAL 5 MINUTE, observed_at) AS bucket_at, max(value) AS cpu_seconds"
        " FROM raw_system_cpu_time GROUP BY ALL"
        "), deltas AS ("
        " SELECT host_name, state, bucket_at,"
        "  cpu_seconds - lag(cpu_seconds) OVER (PARTITION BY host_name, state ORDER BY bucket_at) AS delta_seconds"
        " FROM sampled"
        ") "
        "SELECT host_name, bucket_at,"
        " sum(delta_seconds) FILTER (WHERE state NOT IN ('idle', 'wait')) / sum(delta_seconds) AS busy_share,"
        " sum(delta_seconds) FILTER (WHERE state = 'wait') / sum(delta_seconds) AS iowait_share "
        "FROM deltas WHERE delta_seconds IS NOT NULL AND delta_seconds >= 0 "
        "GROUP BY ALL HAVING sum(delta_seconds) > 0",
    ),
    (
        "memory_usage",
        "CREATE OR REPLACE TABLE memory_usage AS "
        "SELECT host_name, time_bucket(INTERVAL 5 MINUTE, observed_at) AS bucket_at, state,"
        " avg(value) AS bytes_used "
        "FROM raw_system_memory_usage GROUP BY ALL",
    ),
    (
        "filesystem_latest",
        # Latest snapshot per host/mountpoint/state, real filesystems only
        # (the boxes mount tmpfs/overlays that would drown the chart).
        "CREATE OR REPLACE TABLE filesystem_latest AS "
        "WITH latest AS ("
        " SELECT host_name, mountpoint, device, type, state, value,"
        "  row_number() OVER (PARTITION BY host_name, mountpoint, device, state ORDER BY observed_at DESC)"
        "   AS recency_rank"
        " FROM raw_system_filesystem_usage"
        " WHERE type IN ('ext4', 'btrfs', 'xfs', 'zfs')"
        ") "
        "SELECT host_name, mountpoint, device, type,"
        " sum(value) FILTER (WHERE state = 'used') AS used_bytes,"
        " sum(value) FILTER (WHERE state = 'free') AS free_bytes,"
        " sum(value) FILTER (WHERE state = 'used') / nullif(sum(value), 0) AS used_share "
        "FROM latest WHERE recency_rank = 1 GROUP BY ALL",
    ),
    (
        "filesystem_history",
        "CREATE OR REPLACE TABLE filesystem_history AS "
        "SELECT host_name, mountpoint, time_bucket(INTERVAL 30 MINUTE, observed_at) AS bucket_at,"
        " sum(avg_value) FILTER (WHERE state = 'used') / nullif(sum(avg_value), 0) AS used_share "
        "FROM ("
        " SELECT host_name, mountpoint, state, observed_at, avg(value) AS avg_value"
        " FROM raw_system_filesystem_usage WHERE type IN ('ext4', 'btrfs', 'xfs', 'zfs')"
        " GROUP BY ALL"
        ") GROUP BY ALL",
    ),
    (
        "network_throughput",
        # system_network_io is a cumulative byte counter per device/direction;
        # per-bucket deltas over the physical devices become bytes/second.
        "CREATE OR REPLACE TABLE network_throughput AS "
        "WITH sampled AS ("
        " SELECT host_name, device, direction, time_bucket(INTERVAL 5 MINUTE, observed_at) AS bucket_at,"
        "  max(value) AS total_bytes"
        " FROM raw_system_network_io WHERE device != 'lo' GROUP BY ALL"
        "), deltas AS ("
        " SELECT host_name, device, direction, bucket_at,"
        "  total_bytes - lag(total_bytes) OVER (PARTITION BY host_name, device, direction ORDER BY bucket_at)"
        "   AS delta_bytes"
        " FROM sampled"
        ") "
        "SELECT host_name, direction, bucket_at, sum(delta_bytes) / 300.0 AS bytes_per_second "
        "FROM deltas WHERE delta_bytes IS NOT NULL AND delta_bytes >= 0 GROUP BY ALL",
    ),
    (
        "qemu_slices",
        # The box-level qemu process metrics are the per-slice visibility
        # signal (there is deliberately no collector inside the lima VMs).
        "CREATE OR REPLACE TABLE qemu_slices AS "
        "SELECT host_name, bucket_at, count(*) AS qemu_process_count, sum(avg_bytes) AS total_memory_bytes "
        "FROM ("
        " SELECT host_name, process_pid, time_bucket(INTERVAL 5 MINUTE, observed_at) AS bucket_at,"
        "  avg(value) AS avg_bytes"
        " FROM raw_process_memory_usage WHERE process_executable_name LIKE 'qemu%'"
        " GROUP BY ALL"
        ") GROUP BY ALL",
    ),
)


def run_extract(arguments: ExtractArguments) -> None:
    """Raises an AnalyticsError when any step fails.

    Credential, source, and statement failures raise DashboardExtractError; the
    lake session helpers raise their own subclasses (SessionAssemblyError,
    LakeAttachError) for extension, secret, and attach failures.
    """
    credentials = load_env_analytics_credentials(arguments.env_name)
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=arguments.window_days)

    # A stale output file is replaced wholesale so every run is a clean rebuild.
    arguments.output_path.parent.mkdir(parents=True, exist_ok=True)
    arguments.output_path.unlink(missing_ok=True)
    with duckdb.connect(str(arguments.output_path)) as connection:
        connection.execute("SET TimeZone = 'UTC'")
        with log_span("Preparing the DuckDB session (extensions, R2 secret, connector attach)"):
            install_session_extensions(connection, extensions=("httpfs", "postgres"))
            create_r2_secret(
                connection,
                secret_name="observability_logs",
                key_id=credentials.logs_access_key_id,
                secret=credentials.logs_secret_access_key.get_secret_value(),
                account_id=credentials.r2_account_id,
                bucket=credentials.logs_bucket,
            )
            attach_postgres_readonly(connection, alias="rsc", dsn=credentials.rsc_readonly_dsn.get_secret_value())

        with log_span("Materializing the servers table from the env's connector database"):
            server_count = _materialize_servers_table(connection)
        if server_count == 0:
            raise DashboardExtractError(
                f"Env {arguments.env_name!r} has no registered bare-metal servers; nothing to chart"
            )
        logger.info("Found {} bare-metal server(s) registered to {}", server_count, arguments.env_name)

        month_starts = month_starts_within(window_start, window_end)
        for stream_name in _STREAM_NAMES:
            with log_span("Scanning metric stream {}", stream_name):
                _create_stream_view(connection, credentials, stream_name, month_starts, window_start)

        for table_name, statement in _TABLE_STATEMENTS:
            with log_span("Materializing table {}", table_name):
                try:
                    connection.execute(statement)
                except duckdb.Error as e:
                    raise DashboardExtractError(f"Cannot materialize dashboard table {table_name!r}") from e
            row = connection.execute(f"SELECT count(*) FROM {table_name}").fetchone()
            logger.info("Table {} has {} row(s)", table_name, row[0] if row is not None else 0)

        # The raw views reference R2 and the attached Postgres, which are gone
        # once Evidence opens the file; drop them so only the tables remain.
        for stream_name in _STREAM_NAMES:
            connection.execute(f"DROP VIEW IF EXISTS raw_{stream_name}")
    logger.info("Wrote {}", arguments.output_path)


@click.command()
@click.option("--env", "env_name", default=_DEFAULT_ENV_NAME, show_default=True, help="Dev env to extract")
@click.option("--days", "window_days", default=_DEFAULT_WINDOW_DAYS, show_default=True, help="Trailing window, days")
@click.option(
    "--output",
    "output",
    type=click.Path(),
    default=None,
    help="DuckDB file to write (default: data/box_metrics.duckdb next to this script)",
)
def main(env_name: str, window_days: int, output: str | None) -> None:
    setup_logging(level="INFO")
    default_output = Path(__file__).parent / _DEFAULT_OUTPUT_RELATIVE_PATH
    arguments = ExtractArguments(
        env_name=env_name,
        window_days=window_days,
        output_path=Path(output) if output is not None else default_output,
    )
    run_extract(arguments)


if __name__ == "__main__":
    main()
