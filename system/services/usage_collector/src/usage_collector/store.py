"""The workspace's model_call events in SQLite: an append-only event table and per-minute rollups.

An index of the event files (``event_files``), which stay the source of truth.
Every insert is idempotent by ``event_id``, so re-reading a file never
double-counts. The rollup row a new event lands in is updated in the same
transaction, so a totals query over the rollup dimensions reads at most one row
per minute per key instead of scanning raw events. Raw events are kept
``RAW_RETENTION`` and rollups indefinitely; the id of every event ever counted is
kept as long as the rollups, since a file read again from its start (a destroyed
agent's preserved copy, a restored backup) still holds events older than that.
"""

import json
import math
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import auto
from pathlib import Path
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_usage.model_call import ModelCallEvent
from pydantic import Field

from usage_collector.limits import LimitReading

RAW_RETENTION: Final[timedelta] = timedelta(days=90)
# Totals are sums of per-call costs; a nanodollar is far below any price step.
_COST_DECIMAL_PLACES: Final[int] = 9

# Rollup key columns; '' stands for "not known", since NULLs never collide in a unique key.
ROLLUP_DIMENSIONS: Final[tuple[str, ...]] = (
    "origin_kind",
    "origin_id",
    "agent_id",
    "model",
    "billing_mode",
)
# Every field a totals query may group by. The ones outside ROLLUP_DIMENSIONS are read from raw events.
GROUPABLE_DIMENSIONS: Final[tuple[str, ...]] = (
    *ROLLUP_DIMENSIONS,
    "provider",
    "status",
    "run_id",
    "turn_id",
    "parent_agent_id",
    "harness",
    "session_id",
    "call_source",
    "subagent_type",
    "purpose",
    "credential_ref",
    "workspace_id",
    "cost_provenance",
)
# The filters a calls query takes: the rollup dimensions a dashboard groups by, the provider, the
# session, run and turn a call belongs to, and the call itself (a turn of one call with no turn id).
CALL_FILTER_DIMENSIONS: Final[tuple[str, ...]] = (
    *ROLLUP_DIMENSIONS,
    "provider",
    "session_id",
    "run_id",
    "turn_id",
    "event_id",
)
# The billing modes whose cost is spend; a subscription's calls are priced at list price instead.
BILLED_MODES: Final[tuple[str, ...]] = ("IMBUE_KEY", "API_KEY")
# What a turn is grouped by: its turn id, or, for a call no writer gave one, the call alone.
_TURN_KEY: Final[str] = "COALESCE(turn_id, 'call:' || event_id)"
DEFAULT_CALLS_LIMIT: Final[int] = 50
MAX_CALLS_LIMIT: Final[int] = 500
# Harnesses whose start time is read off a log line, not measured, so their durations are approximate.
APPROXIMATE_TIMING_HARNESSES: Final[frozenset[str]] = frozenset({"claude"})
TOKEN_COLUMNS: Final[tuple[str, ...]] = ("input", "output", "cache_read", "cache_write")

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS model_call (
    event_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    input INTEGER,
    output INTEGER,
    cache_read INTEGER,
    cache_write INTEGER,
    cache_write_1h INTEGER,
    cost_usd REAL,
    cost_provenance TEXT,
    billing_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    credential_ref TEXT,
    workspace_id TEXT NOT NULL,
    origin_kind TEXT NOT NULL,
    origin_id TEXT,
    run_id TEXT,
    agent_id TEXT,
    parent_agent_id TEXT,
    harness TEXT,
    session_id TEXT,
    call_source TEXT,
    subagent_type TEXT,
    purpose TEXT,
    event_json TEXT NOT NULL,
    turn_id TEXT
);
CREATE INDEX IF NOT EXISTS model_call_ended_at ON model_call (ended_at);
CREATE TABLE IF NOT EXISTS counted_event (
    event_id TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rollup_minute (
    minute INTEGER NOT NULL,
    origin_kind TEXT NOT NULL,
    origin_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_mode TEXT NOT NULL,
    calls INTEGER NOT NULL,
    unknown_cost_calls INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    input INTEGER NOT NULL,
    output INTEGER NOT NULL,
    cache_read INTEGER NOT NULL,
    cache_write INTEGER NOT NULL,
    PRIMARY KEY (minute, origin_kind, origin_id, agent_id, model, billing_mode)
);
CREATE TABLE IF NOT EXISTS limit_window (
    source TEXT NOT NULL,
    window TEXT NOT NULL,
    label TEXT,
    used_percentage REAL,
    resets_at REAL,
    window_seconds INTEGER,
    observed_at REAL NOT NULL,
    first_used_percentage REAL,
    first_observed_at REAL,
    PRIMARY KEY (source, window)
);
CREATE TABLE IF NOT EXISTS file_position (
    path TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    offset INTEGER NOT NULL
);
"""


class UsageStoreError(Exception):
    """Raised for a totals query the store cannot answer (an unknown dimension or bucket)."""


class TimeBucket(UpperCaseStrEnum):
    MINUTE = auto()
    HOUR = auto()
    DAY = auto()


BUCKET_SECONDS: Final[dict[TimeBucket, int]] = {
    TimeBucket.MINUTE: 60,
    TimeBucket.HOUR: 3600,
    TimeBucket.DAY: 86400,
}


class TotalsRow(FrozenModel):
    """Cost and tokens for one group. ``cost_usd`` sums only calls with a known cost; None when there were none."""

    group: dict[str, str | None] = Field(
        description="The grouped dimensions' values; None for 'not known'"
    )
    bucket_start: datetime | None = Field(
        default=None, description="Start of the time bucket, when bucketed"
    )
    calls: int
    unknown_cost_calls: int = Field(
        description="Calls whose cost could not be determined"
    )
    cost_usd: Decimal | None
    tokens: dict[str, int]


class LatencyRow(FrozenModel):
    """Call durations (ended_at minus started_at) for one group; percentiles cover only the timed calls."""

    group: dict[str, str | None] = Field(
        description="The grouped dimensions' values; None for 'not known'"
    )
    timed_calls: int = Field(description="Calls with a duration above zero")
    untimed_calls: int = Field(
        description="Calls whose writer reported no duration (started_at equal to ended_at)"
    )
    approximate_calls: int = Field(
        description="Timed calls whose start time was read off a log line rather than measured"
    )
    p50_ms: float | None = Field(
        description="Median duration; None with no timed calls"
    )
    p95_ms: float | None = Field(
        description="95th percentile duration; None with no timed calls"
    )


class TurnRow(FrozenModel):
    """One turn's calls, among those a turns query's filters keep: one user message's, or one call's alone."""

    turn_id: str | None = Field(
        description="The turn's id; None for a call no writer gave one"
    )
    event_id: str | None = Field(
        description="For a turn with no turn id, its one call's event id; None otherwise"
    )
    started_at: datetime = Field(description="When the turn's first call started")
    ended_at: datetime = Field(description="When the turn's last call ended")
    calls: int
    failed_calls: int = Field(description="Calls that did not end OK")
    unknown_cost_calls: int = Field(
        description="Calls whose spend is not known: billed with no cost, or of no known billing mode"
    )
    tokens: dict[str, int]
    billed_cost_usd: Decimal | None = Field(
        description="What the Imbue key and API key calls cost; None when there were none priced"
    )
    is_billed_cost_estimated: bool = Field(
        description="Whether any billed cost was estimated from tokens rather than reported"
    )
    subscription_cost_usd: Decimal | None = Field(
        description="The subscription calls at list price, which nothing bills; None when there were none priced"
    )
    models: list[str] = Field(description="The models the turn's calls used")
    origin_kind: str = Field(
        description="What made the turn's calls: a chat, an app, a routine, ..."
    )
    origin_id: str | None


class StoredCall(FrozenModel):
    """One stored model_call event, with its place in the store's insertion order."""

    sequence: int = Field(
        description="Increases with each stored event; the cursor for calls stored after it"
    )
    event: dict[str, object] = Field(description="The model_call event as stored")


def _add_turn_id_column(connection: sqlite3.Connection) -> None:
    """Give a database made before calls had a turn id its column, last, where a new database has it."""
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(model_call)")
    }
    if "turn_id" not in columns:
        connection.execute("ALTER TABLE model_call ADD COLUMN turn_id TEXT")


def _add_first_reading_columns(connection: sqlite3.Connection) -> None:
    """Give a database made before limit windows kept their first reading its columns, last, as a new one has them."""
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(limit_window)")
    }
    if "first_used_percentage" not in columns:
        connection.execute(
            "ALTER TABLE limit_window ADD COLUMN first_used_percentage REAL"
        )
        connection.execute("ALTER TABLE limit_window ADD COLUMN first_observed_at REAL")


def _rollup_key(event: ModelCallEvent) -> tuple[str, ...]:
    attribution = event.attribution
    return (
        str(attribution.origin_kind),
        attribution.origin_id or "",
        attribution.agent_id or "",
        event.model or "",
        str(event.billing_mode),
    )


class UsageStore:
    """One SQLite database, shared by the ingest, query and stream threads behind a lock."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            database_path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
            _add_turn_id_column(self._connection)
            _add_first_reading_columns(self._connection)
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS model_call_turn_id ON model_call (turn_id)"
            )
            # A database made before counted_event existed: its stored events are the ones counted so far.
            self._connection.execute(
                "INSERT OR IGNORE INTO counted_event SELECT event_id FROM model_call"
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def insert(self, event: ModelCallEvent) -> bool:
        """Store ``event`` and fold it into its rollup. Returns False when its event_id was already stored."""
        attribution = event.attribution
        tokens = event.tokens
        cost = float(event.cost_usd) if event.cost_usd is not None else None
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO counted_event VALUES (?)", (event.event_id,)
                )
                is_new = cursor.rowcount == 1
                if is_new:
                    self._connection.execute(
                        "INSERT INTO model_call VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            event.event_id,
                            event.started_at.timestamp(),
                            event.ended_at.timestamp(),
                            event.provider,
                            event.model,
                            tokens.input,
                            tokens.output,
                            tokens.cache_read,
                            tokens.cache_write,
                            tokens.cache_write_1h,
                            cost,
                            _optional_str(event.cost_provenance),
                            str(event.billing_mode),
                            str(event.status),
                            event.credential_ref,
                            attribution.workspace_id,
                            str(attribution.origin_kind),
                            attribution.origin_id,
                            attribution.run_id,
                            attribution.agent_id,
                            attribution.parent_agent_id,
                            attribution.harness,
                            attribution.session_id,
                            _optional_str(attribution.call_source),
                            attribution.subagent_type,
                            _optional_str(attribution.purpose),
                            event.model_dump_json(),
                            attribution.turn_id,
                        ),
                    )
                    self._connection.execute(
                        "INSERT INTO rollup_minute VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?) "
                        "ON CONFLICT (minute, origin_kind, origin_id, agent_id, model, billing_mode) DO UPDATE SET "
                        "calls = calls + 1, unknown_cost_calls = unknown_cost_calls + excluded.unknown_cost_calls, "
                        "cost_usd = cost_usd + excluded.cost_usd, input = input + excluded.input, "
                        "output = output + excluded.output, cache_read = cache_read + excluded.cache_read, "
                        "cache_write = cache_write + excluded.cache_write",
                        (
                            int(event.ended_at.timestamp()) // 60,
                            *_rollup_key(event),
                            1 if cost is None else 0,
                            cost or 0.0,
                            tokens.input or 0,
                            tokens.output or 0,
                            tokens.cache_read or 0,
                            tokens.cache_write or 0,
                        ),
                    )
                self._connection.execute("COMMIT")
            except sqlite3.Error:
                self._connection.execute("ROLLBACK")
                raise
        return is_new

    def record_limits(self, readings: Sequence[LimitReading]) -> None:
        """Keep each reading unless the store already holds a better one for its source and window.

        Within one window (the same reset time) the higher share wins: a share only grows until its window resets,
        and every agent on the account reports the account's windows, so a lower share there is a stale reading,
        however recently it was stamped. A later reset time is a later window and wins; an earlier one loses.
        Without a reset time on either side, the fresher reading wins.

        Beside it each window keeps its first reading: the lowest share seen in it, at the earliest time that share
        was seen (an idle agent can keep restamping an old share, which must not move it later). A later window
        starts over from the reading that opens it. The rise from the first reading to the kept one is what this
        workspace's calls between them added to the window, which is what a call's share is estimated from.
        """
        if not readings:
            return
        rows = [
            {
                "source": reading.source,
                "window": reading.window,
                "label": reading.label,
                "used": reading.used_percentage,
                "resets_at": reading.resets_at.timestamp()
                if reading.resets_at is not None
                else None,
                "window_seconds": reading.window_seconds,
                "observed_at": reading.observed_at.timestamp(),
            }
            for reading in readings
        ]
        # A reading dated before its window opened was stamped onto an older call after the fact (a writer catching
        # up on old session logs stamps them with the reading it holds now), so it cannot say where the window began.
        is_in_window = "(:resets_at IS NULL OR :window_seconds IS NULL OR :observed_at >= :resets_at - :window_seconds)"
        # Run before the upsert, so a reading is weighed against the window as it stood.
        update_first = (
            "UPDATE limit_window SET "
            f"first_used_percentage = CASE WHEN {is_in_window} THEN :used END, "
            f"first_observed_at = CASE WHEN {is_in_window} THEN :observed_at END "
            "WHERE source = :source AND window = :window AND CASE "
            "WHEN :resets_at IS NULL OR resets_at IS NULL THEN :observed_at >= observed_at "
            "WHEN :resets_at != resets_at THEN :resets_at > resets_at "
            f"WHEN :used IS NULL OR NOT {is_in_window} THEN 0 "
            "ELSE first_used_percentage IS NULL OR :used < first_used_percentage "
            "OR (:used = first_used_percentage AND :observed_at < first_observed_at) "
            "END"
        )
        upsert = (
            "INSERT INTO limit_window VALUES "
            "(:source, :window, :label, :used, :resets_at, :window_seconds, :observed_at, "
            f"CASE WHEN {is_in_window} THEN :used END, CASE WHEN {is_in_window} THEN :observed_at END) "
            "ON CONFLICT (source, window) DO UPDATE SET label = excluded.label, "
            "used_percentage = excluded.used_percentage, resets_at = excluded.resets_at, "
            "window_seconds = excluded.window_seconds, observed_at = excluded.observed_at "
            "WHERE CASE "
            "WHEN excluded.resets_at IS NULL OR limit_window.resets_at IS NULL "
            "THEN excluded.observed_at >= limit_window.observed_at "
            "WHEN excluded.resets_at != limit_window.resets_at "
            "THEN excluded.resets_at > limit_window.resets_at "
            "WHEN excluded.used_percentage IS limit_window.used_percentage "
            "THEN excluded.observed_at >= limit_window.observed_at "
            "ELSE COALESCE(excluded.used_percentage, -1) > COALESCE(limit_window.used_percentage, -1) "
            "END"
        )
        # One reading at a time, so a later reading of the same window in this batch sees what the earlier one left.
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    self._connection.execute(update_first, row)
                    self._connection.execute(upsert, row)
                self._connection.execute("COMMIT")
            except sqlite3.Error:
                self._connection.execute("ROLLBACK")
                raise

    def limits(self, since: datetime) -> list[LimitReading]:
        """The freshest reading of every window reported since ``since``, by source and then window length."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM limit_window WHERE observed_at >= ? "
                "ORDER BY source, COALESCE(window_seconds, 0), window",
                (since.timestamp(),),
            ).fetchall()
        return [
            LimitReading(
                source=row["source"],
                window=row["window"],
                label=row["label"],
                used_percentage=row["used_percentage"],
                resets_at=(
                    datetime.fromtimestamp(row["resets_at"], tz=timezone.utc)
                    if row["resets_at"] is not None
                    else None
                ),
                window_seconds=row["window_seconds"],
                observed_at=datetime.fromtimestamp(row["observed_at"], tz=timezone.utc),
                first_used_percentage=row["first_used_percentage"],
                first_observed_at=(
                    datetime.fromtimestamp(row["first_observed_at"], tz=timezone.utc)
                    if row["first_observed_at"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def file_position(self, path: Path) -> tuple[int | None, int]:
        """The inode and byte offset the collector has read ``path`` up to; (None, 0) for a new file."""
        with self._lock:
            row = self._connection.execute(
                "SELECT inode, offset FROM file_position WHERE path = ?", (str(path),)
            ).fetchone()
        return (None, 0) if row is None else (row["inode"], row["offset"])

    def set_file_position(self, path: Path, inode: int, offset: int) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO file_position VALUES (?, ?, ?) "
                "ON CONFLICT (path) DO UPDATE SET inode = excluded.inode, offset = excluded.offset",
                (str(path), inode, offset),
            )

    def prune(self, now: datetime) -> int:
        """Delete raw events past ``RAW_RETENTION``; their rollups stay. Returns how many were deleted."""
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM model_call WHERE ended_at < ?",
                ((now - RAW_RETENTION).timestamp(),),
            )
        return cursor.rowcount

    def totals(
        self,
        group_by: Sequence[str],
        since: datetime,
        until: datetime | None = None,
        bucket: TimeBucket | None = None,
    ) -> list[TotalsRow]:
        """Cost and token totals for calls that ended in [since, until), grouped by ``group_by`` (and ``bucket``)."""
        dimensions = _checked_dimensions(group_by)
        is_rollup = all(dimension in ROLLUP_DIMENSIONS for dimension in dimensions)
        if is_rollup:
            table, time_expression = "rollup_minute", "minute * 60"
            sums = [
                "SUM(calls)",
                "SUM(unknown_cost_calls)",
                "SUM(cost_usd)",
                *[f"SUM({c})" for c in TOKEN_COLUMNS],
            ]
            # A rollup's cost sum is 0.0, not NULL, when every call in it was unpriced.
            known_cost = "SUM(calls) - SUM(unknown_cost_calls)"
        else:
            table, time_expression = "model_call", "ended_at"
            sums = [
                "COUNT(*)",
                "SUM(cost_usd IS NULL)",
                "SUM(cost_usd)",
                *[f"COALESCE(SUM({c}), 0)" for c in TOKEN_COLUMNS],
            ]
            known_cost = "COUNT(cost_usd)"
        selected = list(dimensions)
        if bucket is not None:
            size = BUCKET_SECONDS[bucket]
            selected.append(
                f"(CAST({time_expression} AS INTEGER) / {size}) * {size} AS bucket_start"
            )
        group_clause = ", ".join(
            [*dimensions, *(["bucket_start"] if bucket is not None else [])]
        )
        query = (
            f"SELECT {', '.join([*selected, *sums, known_cost])} FROM {table} "
            f"WHERE {time_expression} >= ? AND {time_expression} < ?"
            + (
                f" GROUP BY {group_clause} ORDER BY {group_clause}"
                if group_clause
                else ""
            )
        )
        with self._lock:
            rows = self._connection.execute(
                query, (since.timestamp(), _until_timestamp(until))
            ).fetchall()
        return [
            _totals_row(row, dimensions, bucket is not None, is_rollup) for row in rows
        ]

    def latency(
        self,
        group_by: Sequence[str],
        since: datetime,
        until: datetime | None = None,
        filters: Mapping[str, Sequence[str]] | None = None,
    ) -> list[LatencyRow]:
        """Duration percentiles for calls that ended in [since, until), grouped by ``group_by``, from raw events.

        ``filters`` keeps only calls whose field is one of the given values ("" for "not known"). Percentiles do not
        add up across groups, so a reader that needs one over several groups (a provider's calls on any of several
        billing modes) asks for it here rather than combining rows.
        """
        dimensions = _checked_dimensions(group_by)
        selected = ", ".join(
            [*dimensions, "(ended_at - started_at) * 1000 AS duration_ms", "harness"]
        )
        _checked_dimensions(list(filters or {}))
        filter_clauses, filter_parameters = _any_of(filters or {})
        clauses = ["ended_at >= ?", "ended_at < ?", *filter_clauses]
        parameters: list[object] = [
            since.timestamp(),
            _until_timestamp(until),
            *filter_parameters,
        ]
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {selected} FROM model_call WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchall()
        durations: dict[tuple[str | None, ...], list[float]] = {}
        untimed: dict[tuple[str | None, ...], int] = {}
        approximate: dict[tuple[str | None, ...], int] = {}
        for row in rows:
            key = tuple(row[dimension] for dimension in dimensions)
            durations.setdefault(key, [])
            if row["duration_ms"] <= 0:
                untimed[key] = untimed.get(key, 0) + 1
                continue
            durations[key].append(row["duration_ms"])
            if row["harness"] in APPROXIMATE_TIMING_HARNESSES:
                approximate[key] = approximate.get(key, 0) + 1
        return [
            LatencyRow(
                group=dict(zip(dimensions, key)),
                timed_calls=len(values),
                untimed_calls=untimed.get(key, 0),
                approximate_calls=approximate.get(key, 0),
                p50_ms=_percentile(values, 50),
                p95_ms=_percentile(values, 95),
            )
            for key, values in sorted(
                durations.items(), key=lambda item: [str(part) for part in item[0]]
            )
        ]

    def calls(
        self,
        filters: Mapping[str, Sequence[str]],
        limit: int = DEFAULT_CALLS_LIMIT,
        before: str | None = None,
        after: int | None = None,
        offset: int = 0,
    ) -> list[StoredCall]:
        """Stored calls whose fields take one of the values ``filters`` gives them ("" for "not known"), newest first.

        ``before`` (an event id) pages back to calls that ended before that one;
        ``after`` (a sequence) returns only calls stored after it, which is how a
        stream client fills the gap a reconnect left. ``offset`` skips that many
        of the newest, for numbered pages.
        """
        if not 1 <= limit <= MAX_CALLS_LIMIT:
            raise UsageStoreError(f"limit must be between 1 and {MAX_CALLS_LIMIT}")
        if offset < 0:
            raise UsageStoreError("offset must not be negative")
        clauses, parameters = _call_filters(filters)
        with self._lock:
            if before is not None:
                anchor = self._connection.execute(
                    "SELECT ended_at, rowid FROM model_call WHERE event_id = ?",
                    (before,),
                ).fetchone()
                if anchor is None:
                    raise UsageStoreError(f"No stored call has event id {before!r}")
                clauses.append("(ended_at, rowid) < (?, ?)")
                parameters.extend([anchor["ended_at"], anchor["rowid"]])
            if after is not None:
                clauses.append("rowid > ?")
                parameters.append(after)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = self._connection.execute(
                f"SELECT rowid, event_json FROM model_call{where} "
                "ORDER BY ended_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [
            StoredCall(sequence=row["rowid"], event=json.loads(row["event_json"]))
            for row in rows
        ]

    def turns(
        self,
        filters: Mapping[str, Sequence[str]],
        limit: int = DEFAULT_CALLS_LIMIT,
        offset: int = 0,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[TurnRow]:
        """The turns of the calls ``filters`` keep, latest-ending first, each summing only those calls.

        A call with no turn id is a turn of its own. ``offset`` skips that many of the latest turns. ``since`` and
        ``until`` keep only calls that ended in [since, until), so a turn is in a window when any of its calls is.
        """
        if not 1 <= limit <= MAX_CALLS_LIMIT:
            raise UsageStoreError(f"limit must be between 1 and {MAX_CALLS_LIMIT}")
        if offset < 0:
            raise UsageStoreError("offset must not be negative")
        clauses, parameters = _turn_filters(filters, since, until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        billed = ", ".join(f"'{mode}'" for mode in BILLED_MODES)
        tokens = ", ".join(f"SUM(COALESCE({column}, 0))" for column in TOKEN_COLUMNS)
        query = (
            f"SELECT MIN(turn_id), CASE WHEN COUNT(turn_id) = 0 THEN MIN(event_id) END, "
            "MIN(started_at), MAX(ended_at), COUNT(*), SUM(status != 'OK'), "
            f"SUM(CASE WHEN billing_mode IN ({billed}) THEN cost_usd IS NULL "
            "WHEN billing_mode = 'SUBSCRIPTION' THEN 0 ELSE 1 END), "
            f"{tokens}, "
            f"SUM(CASE WHEN billing_mode IN ({billed}) THEN cost_usd END), "
            f"MAX(billing_mode IN ({billed}) AND cost_usd > 0 AND cost_provenance = 'ESTIMATED'), "
            "SUM(CASE WHEN billing_mode = 'SUBSCRIPTION' THEN cost_usd END), "
            "GROUP_CONCAT(DISTINCT model), MIN(origin_kind), MIN(origin_id) "
            f"FROM model_call{where} GROUP BY {_TURN_KEY} "
            f"ORDER BY MAX(ended_at) DESC, {_TURN_KEY} LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self._connection.execute(
                query, (*parameters, limit, offset)
            ).fetchall()
        return [_turn_row(list(row)) for row in rows]

    def count_turns(
        self,
        filters: Mapping[str, Sequence[str]],
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """How many turns ``turns`` would page through for ``filters``, ``since`` and ``until``."""
        clauses, parameters = _turn_filters(filters, since, until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._connection.execute(
                f"SELECT COUNT(DISTINCT {_TURN_KEY}) FROM model_call{where}", parameters
            ).fetchone()
        return int(row[0])

    def count_calls(self, filters: Mapping[str, Sequence[str]]) -> int:
        """How many stored calls ``calls`` would page through for ``filters``."""
        clauses, parameters = _call_filters(filters)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._connection.execute(
                f"SELECT COUNT(*) FROM model_call{where}", parameters
            ).fetchone()
        return int(row[0])


def _totals_row(
    row: sqlite3.Row, dimensions: list[str], is_bucketed: bool, is_rollup: bool
) -> TotalsRow:
    values = list(row)
    group = {
        dimension: (None if is_rollup and values[index] == "" else values[index])
        for index, dimension in enumerate(dimensions)
    }
    offset = len(dimensions)
    bucket_start = None
    if is_bucketed:
        bucket_start = datetime.fromtimestamp(values[offset], tz=timezone.utc)
        offset += 1
    calls, unknown_cost_calls, cost, *token_sums, known_cost_calls = values[offset:]
    return TotalsRow(
        group=group,
        bucket_start=bucket_start,
        calls=calls or 0,
        unknown_cost_calls=unknown_cost_calls or 0,
        cost_usd=_summed_cost(cost) if known_cost_calls and cost is not None else None,
        tokens=dict(zip(TOKEN_COLUMNS, (int(value or 0) for value in token_sums))),
    )


def _checked_dimensions(group_by: Sequence[str]) -> list[str]:
    """``group_by`` without repeats; raises UsageStoreError for a field a query cannot group by."""
    unknown = [
        dimension for dimension in group_by if dimension not in GROUPABLE_DIMENSIONS
    ]
    if unknown:
        raise UsageStoreError(
            f"Cannot group by {unknown}; choose from {list(GROUPABLE_DIMENSIONS)}"
        )
    return list(dict.fromkeys(group_by))


def _call_filters(
    filters: Mapping[str, Sequence[str]],
) -> tuple[list[str], list[object]]:
    unknown = [
        dimension for dimension in filters if dimension not in CALL_FILTER_DIMENSIONS
    ]
    if unknown:
        raise UsageStoreError(
            f"Cannot filter by {unknown}; choose from {list(CALL_FILTER_DIMENSIONS)}"
        )
    return _any_of(filters)


def _turn_filters(
    filters: Mapping[str, Sequence[str]],
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[str], list[object]]:
    """``_call_filters``, plus a bound on when each kept call ended where ``since`` or ``until`` is given."""
    clauses, parameters = _call_filters(filters)
    if since is not None:
        clauses.append("ended_at >= ?")
        parameters.append(since.timestamp())
    if until is not None:
        clauses.append("ended_at < ?")
        parameters.append(until.timestamp())
    return clauses, parameters


def _any_of(filters: Mapping[str, Sequence[str]]) -> tuple[list[str], list[object]]:
    """SQL clauses keeping rows whose field is one of the given values, "" standing for NULL."""
    clauses: list[str] = []
    parameters: list[object] = []
    for dimension, values in filters.items():
        if not values:
            raise UsageStoreError(f"Filter {dimension} has no values")
        known = [value for value in values if value]
        alternatives = (
            [f"{dimension} IN ({', '.join('?' for _ in known)})"] if known else []
        )
        if len(known) < len(values):
            alternatives.append(f"{dimension} IS NULL")
        clauses.append(f"({' OR '.join(alternatives)})")
        parameters.extend(known)
    return clauses, parameters


def _until_timestamp(until: datetime | None) -> float:
    """The window's end; an open window also takes calls whose clock ran a little ahead."""
    return (until or datetime.now(timezone.utc) + timedelta(days=1)).timestamp()


def _percentile(values: list[float], percent: int) -> float | None:
    """The nearest-rank percentile of ``values``; None when there are none."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100 * len(ordered)))
    return ordered[rank - 1]


def _turn_row(values: list[object]) -> TurnRow:
    turn_id, event_id, started_at, ended_at, calls, failed, unknown = values[:7]
    token_sums = values[7 : 7 + len(TOKEN_COLUMNS)]
    billed, estimated, subscription, models, origin_kind, origin_id = values[
        7 + len(TOKEN_COLUMNS) :
    ]
    return TurnRow(
        turn_id=_optional_str(turn_id),
        event_id=_optional_str(event_id),
        started_at=datetime.fromtimestamp(float(str(started_at)), tz=timezone.utc),
        ended_at=datetime.fromtimestamp(float(str(ended_at)), tz=timezone.utc),
        calls=int(str(calls)),
        failed_calls=int(str(failed)),
        unknown_cost_calls=int(str(unknown)),
        tokens={
            column: int(str(total)) for column, total in zip(TOKEN_COLUMNS, token_sums)
        },
        billed_cost_usd=None if billed is None else _summed_cost(float(str(billed))),
        is_billed_cost_estimated=bool(estimated),
        subscription_cost_usd=(
            None if subscription is None else _summed_cost(float(str(subscription)))
        ),
        models=[] if models is None else sorted(str(models).split(",")),
        origin_kind=str(origin_kind),
        origin_id=_optional_str(origin_id),
    )


def _optional_str(value: object | None) -> str | None:
    return None if value is None else str(value)


def _summed_cost(cost: float) -> Decimal:
    """A SQLite sum as dollars, without the float noise summing adds (0.0016, not 0.0015999999999999999)."""
    return Decimal(repr(round(cost, _COST_DECIMAL_PLACES)))
