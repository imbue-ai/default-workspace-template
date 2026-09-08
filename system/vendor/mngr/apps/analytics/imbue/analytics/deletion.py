"""The account-deletion path for collected data.

Only account deletion deletes (plan departure merely stops collection): the
account's transcript-lake rows are DELETEd -- unqueryable immediately,
physically removed once the covering DuckLake snapshots expire during
lake_maintenance (within the 30-day bound) -- and one deletion_events fact
row is appended to the ops database. Metrics-lake raw and gold rows survive,
keyed by the now-orphaned opaque account id, so aggregate history (including
"how many accounts deleted") stays answerable.

Invoked by the operator tool ``scripts/delete_accounts.py`` as part of its
per-account cascade.
"""

import logging
from datetime import datetime
from typing import Any

import duckdb

import imbue.analytics.ops_db as ops_db
from imbue.analytics.errors import DeletionError
from imbue.analytics.lake import TRANSCRIPTS_RAW_EVENTS_TABLE

logger = logging.getLogger(__name__)


def count_account_transcript_rows(lake_connection: Any, account_id: str) -> int:
    """Raises DeletionError when the transcripts lake cannot be queried."""
    try:
        row = lake_connection.execute(
            f"SELECT count(*) FROM {TRANSCRIPTS_RAW_EVENTS_TABLE} WHERE account_id = ?", (account_id,)
        ).fetchone()
    except duckdb.Error as e:
        raise DeletionError("Cannot count the account's transcript rows") from e
    return int(row[0]) if row is not None else 0


def delete_account_transcripts(
    lake_connection: Any,
    ops_connection: Any,
    account_id: str,
    now: datetime,
) -> int:
    """Delete the account's transcript-lake content and record the deletion fact.

    Returns the number of rows deleted. Idempotent: a re-run deletes zero rows
    and appends another (zero-count) fact row, so a partially-failed cascade
    can simply be re-driven. Raises DeletionError when the DELETE fails --
    the fact row is only written after the DELETE commits.
    """
    row_count = count_account_transcript_rows(lake_connection, account_id)
    try:
        lake_connection.execute(f"DELETE FROM {TRANSCRIPTS_RAW_EVENTS_TABLE} WHERE account_id = ?", (account_id,))
    except duckdb.Error as e:
        raise DeletionError("Cannot delete the account's transcript rows") from e
    ops_db.record_deletion_event(
        ops_connection,
        account_id=account_id,
        requested_at=now,
        transcript_rows_deleted=row_count,
        detail="",
    )
    logger.info("Deleted %d transcript rows for one account", row_count)
    return row_count
