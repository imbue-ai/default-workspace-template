"""Ops-database access: job-run bookkeeping and the collection loop's state.

Called through the module (``ops_db.get_ops_db_connection(...)``) so tests can
substitute the connection factory at a single seam. Writes go through psycopg2
(the aggregation session attaches the same database read-only as ``ops``).
"""

from datetime import datetime
from typing import Any

import psycopg2


def get_ops_db_connection(dsn: str) -> Any:
    return psycopg2.connect(dsn)


def record_job_run(
    connection: Any,
    job_name: str,
    started_at: datetime,
    finished_at: datetime,
    is_success: bool,
    detail: str,
) -> None:
    """Append one row to job_runs (the append-only source for pipeline_health)."""
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO job_runs (job_name, started_at, finished_at, is_success, detail)"
                " VALUES (%s, %s, %s, %s, %s)",
                (job_name, started_at, finished_at, is_success, detail),
            )


def read_consent_ledger(connection: Any) -> dict[str, bool]:
    """Current is_consenting flag per account id, as last synced from the connector DB."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT account_id, is_consenting FROM consent_ledger")
        return {row[0]: bool(row[1]) for row in cursor.fetchall()}


def set_consent(connection: Any, account_id: str, is_consenting: bool, now: datetime) -> None:
    """Upsert one account's consent state; first_consented_at survives later flips."""
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO consent_ledger (account_id, is_consenting, first_consented_at, last_changed_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (account_id) DO UPDATE"
                " SET is_consenting = EXCLUDED.is_consenting, last_changed_at = EXCLUDED.last_changed_at",
                (account_id, is_consenting, now, now),
            )


def read_cursors_for_host(connection: Any, host_id: str) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT source, cursor FROM collection_cursors WHERE host_id = %s", (host_id,))
        return {row[0]: row[1] for row in cursor.fetchall()}


def write_cursor(connection: Any, host_id: str, source: str, cursor_value: str, now: datetime) -> None:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO collection_cursors (host_id, source, cursor, updated_at)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (host_id, source) DO UPDATE"
                " SET cursor = EXCLUDED.cursor, updated_at = EXCLUDED.updated_at",
                (host_id, source, cursor_value, now),
            )


def read_host_keys(connection: Any, host_id: str) -> dict[str, str]:
    """Last-seen sshd host public key per endpoint ('container' / 'vm') for one host."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT endpoint, host_public_key FROM collection_host_keys WHERE host_id = %s", (host_id,))
        return {row[0]: row[1] for row in cursor.fetchall()}


def record_host_key(connection: Any, host_id: str, endpoint: str, host_public_key: str, now: datetime) -> None:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO collection_host_keys (host_id, endpoint, host_public_key, first_seen_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (host_id, endpoint) DO UPDATE"
                " SET host_public_key = EXCLUDED.host_public_key, updated_at = EXCLUDED.updated_at",
                (host_id, endpoint, host_public_key, now, now),
            )


def read_last_collection_attempts(connection: Any) -> dict[str, datetime]:
    """The most recent collection attempt per host id (any outcome), for the at-most-hourly gate."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT host_id, max(started_at) FROM collection_runs GROUP BY host_id")
        return {row[0]: row[1] for row in cursor.fetchall()}


def record_collection_run(
    connection: Any,
    run_id: str,
    host_id: str,
    account_id: str,
    started_at: datetime,
    finished_at: datetime,
    outcome: str,
    script_version: str,
    metrics_rows: int,
    transcript_rows: int,
    dropped_lines: int,
    stdout_bytes: int,
    is_host_key_changed: bool,
    detail: str,
) -> None:
    """Append one workspace collection attempt to the server-side audit."""
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO collection_runs (run_id, host_id, account_id, started_at, finished_at, outcome,"
                " script_version, metrics_rows, transcript_rows, dropped_lines, stdout_bytes,"
                " is_host_key_changed, detail)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    host_id,
                    account_id,
                    started_at,
                    finished_at,
                    outcome,
                    script_version,
                    metrics_rows,
                    transcript_rows,
                    dropped_lines,
                    stdout_bytes,
                    is_host_key_changed,
                    detail,
                ),
            )


def record_deletion_event(
    connection: Any,
    account_id: str,
    requested_at: datetime,
    transcript_rows_deleted: int,
    detail: str,
) -> None:
    """Append one account-deletion fact row (the transcript-lake DELETE already ran)."""
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO deletion_events (account_id, requested_at, transcript_rows_deleted, detail)"
                " VALUES (%s, %s, %s, %s)",
                (account_id, requested_at, transcript_rows_deleted, detail),
            )
