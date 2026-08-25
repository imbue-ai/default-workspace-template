"""The collection script's output protocol, as validated by the runner.

The injected script emits ONE multiplexed JSONL stream on stdout: every line
is ``{"source": <feed>, "record": {...}}`` where the record carries the
standard event envelope (``timestamp``/``event_id``/``type``/``source``), and
the stream is terminated by a single ``{"source": "run_summary", ...}`` line
(counts, new cursors, script version, budget state).

The runner treats that stream as UNTRUSTED workspace output: it validates the
envelope shape and size caps here and never inspects or transforms record
payloads beyond that. Oversize, malformed, or unknown-source lines are dropped
and counted, never parsed further, and never logged (payload content must not
reach the cron's own logs). The script's emit code cannot import this module
(it runs standalone inside the workspace); the emitter/parser conformance
tests in ``protocol_test.py`` are what keep the two sides in step.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any
from typing import Final

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

logger = logging.getLogger(__name__)

# Per-line and per-field caps. A line larger than the cap is dropped without
# JSON-parsing it (bounding parser memory on hostile input).
MAX_LINE_BYTES: Final[int] = 1024 * 1024
MAX_RECORDS_PER_RUN: Final[int] = 500_000
_MAX_ENVELOPE_FIELD_CHARS: Final[int] = 256
_MAX_ERROR_DETAIL_CHARS: Final[int] = 500

# Feed names the script may emit. Transcript records go to the transcripts
# lake; every other known source goes to the metrics lake.
TRANSCRIPTS_SOURCE: Final[str] = "transcripts"
RUN_SUMMARY_SOURCE: Final[str] = "run_summary"
METRICS_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "client_activity",
        "services",
        "servers",
        "git_numstat",
        "workspace_state",
        "latchkey_state",
    }
)

# Event timestamps arrive as nanosecond-precision ISO 8601 strings;
# datetime.fromisoformat only accepts up to microseconds, so the fraction is
# trimmed before parsing.
_FRACTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"\.(\d{6})\d+")


class CollectedRecord(BaseModel):
    """One validated event from the stream: typed envelope columns plus the raw payload."""

    model_config = ConfigDict(frozen=True)

    feed_source: str = Field(description="The multiplexed feed the line arrived on")
    timestamp: datetime = Field(description="The record's own event timestamp (UTC)")
    event_id: str = Field(description="The record's unique event id (the downstream dedupe key)")
    record_type: str = Field(description="The record's declared event type")
    record_source: str = Field(description="The record's declared event source")
    payload: str = Field(description="The full original record as compact JSON")


class RunSummary(BaseModel):
    """The stream's terminating line: what the script says it did."""

    model_config = ConfigDict(frozen=True)

    record_count_by_source: dict[str, int] = Field(description="Emitted record counts per feed")
    cursor_by_source: dict[str, str] = Field(description="New cursor value per feed (validated JSON-object strings)")
    error_by_source: dict[str, str] = Field(description="Per-feed failure summaries (bounded strings)")
    read_bytes: int = Field(description="Pre-redaction input bytes the script consumed")
    is_budget_exhausted: bool = Field(description="Whether the input budget stopped the run early")
    script_version: str = Field(description="Content hash the runner stamped into the script invocation")


class ParsedCollectionOutput(BaseModel):
    """Everything one script run's stdout validated into."""

    model_config = ConfigDict(frozen=True)

    metrics_records: tuple[CollectedRecord, ...] = Field(description="Validated non-transcript records")
    transcript_records: tuple[CollectedRecord, ...] = Field(description="Validated transcript records")
    run_summary: RunSummary | None = Field(description="The terminating summary; None when absent/invalid")
    dropped_line_count: int = Field(description="Lines dropped for size, shape, or unknown source")


def parse_event_timestamp(raw_timestamp: str) -> datetime | None:
    """Parse an ISO 8601 timestamp, tolerating nanosecond fractions and a Z suffix."""
    trimmed = _FRACTION_PATTERN.sub(r".\1", raw_timestamp.strip())
    try:
        parsed = datetime.fromisoformat(trimmed.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _bounded_str(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_ENVELOPE_FIELD_CHARS:
        return None
    return value


def _validate_record_line(parsed_line: dict[str, Any], feed_source: str) -> CollectedRecord | None:
    record = parsed_line.get("record")
    if not isinstance(record, dict):
        return None
    event_id = _bounded_str(record.get("event_id"))
    record_type = _bounded_str(record.get("type"))
    record_source = _bounded_str(record.get("source"))
    raw_timestamp = _bounded_str(record.get("timestamp"))
    if event_id is None or record_type is None or record_source is None or raw_timestamp is None:
        return None
    timestamp = parse_event_timestamp(raw_timestamp)
    if timestamp is None:
        return None
    return CollectedRecord(
        feed_source=feed_source,
        timestamp=timestamp,
        event_id=event_id,
        record_type=record_type,
        record_source=record_source,
        payload=json.dumps(record, separators=(",", ":"), sort_keys=True),
    )


def _validated_cursor_by_source(raw_cursor_by_source: dict[Any, Any]) -> dict[str, str]:
    """Keep only known-feed cursor entries whose value is a string encoding a JSON object.

    The runner persists these values verbatim (keyed by source) and replays
    them into the next run's cursors file, so anything else from the untrusted
    stream is dropped here (the feed just re-collects, deduped by event id
    downstream). Unknown source names are dropped exactly like unknown-source
    record lines, so a hostile summary cannot grow the cursor table.
    """
    cursor_by_source: dict[str, str] = {}
    for key, value in raw_cursor_by_source.items():
        if key != TRANSCRIPTS_SOURCE and key not in METRICS_SOURCES:
            logger.warning("Dropped one run-summary cursor entry for an unknown source (content withheld)")
            continue
        parsed_cursor: Any = None
        if isinstance(value, str):
            try:
                parsed_cursor = json.loads(value)
            except json.JSONDecodeError:
                # Content is untrusted and must never reach our logs.
                logger.warning("Dropped one run-summary cursor entry that was not valid JSON (content withheld)")
                continue
        if isinstance(parsed_cursor, dict):
            cursor_by_source[str(key)] = value
        else:
            logger.warning("Dropped one run-summary cursor entry that was not a JSON-object string")
    return cursor_by_source


def _validate_run_summary(parsed_line: dict[str, Any]) -> RunSummary | None:
    try:
        return RunSummary(
            record_count_by_source={
                str(key): int(value) for key, value in dict(parsed_line.get("record_count_by_source") or {}).items()
            },
            cursor_by_source=_validated_cursor_by_source(dict(parsed_line.get("cursor_by_source") or {})),
            error_by_source={
                str(key): str(value)[:_MAX_ERROR_DETAIL_CHARS]
                for key, value in dict(parsed_line.get("error_by_source") or {}).items()
            },
            read_bytes=int(parsed_line.get("read_bytes", 0)),
            is_budget_exhausted=bool(parsed_line.get("is_budget_exhausted", False)),
            script_version=str(parsed_line.get("script_version", "")),
        )
    except (TypeError, ValueError, ValidationError):
        return None


def parse_collection_output(stdout_text: str) -> ParsedCollectionOutput:
    """Validate one run's full stdout into typed records; never raises on content."""
    metrics_records: list[CollectedRecord] = []
    transcript_records: list[CollectedRecord] = []
    run_summary: RunSummary | None = None
    dropped_line_count = 0
    record_count = 0
    for line in stdout_text.splitlines():
        if not line.strip():
            continue
        if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
            dropped_line_count += 1
            continue
        try:
            parsed_line = json.loads(line)
        except json.JSONDecodeError:
            # Content is untrusted and must never reach our logs; warn once
            # per run so the corruption is visible, count the rest.
            if dropped_line_count == 0:
                logger.warning("Dropped at least one malformed collection output line (content withheld)")
            dropped_line_count += 1
            continue
        if not isinstance(parsed_line, dict):
            dropped_line_count += 1
            continue
        feed_source = parsed_line.get("source")
        if feed_source == RUN_SUMMARY_SOURCE:
            summary = _validate_run_summary(parsed_line)
            if summary is None:
                dropped_line_count += 1
            else:
                # Last summary wins; a forged early summary is superseded by
                # the script's real terminating line.
                run_summary = summary
            continue
        if record_count >= MAX_RECORDS_PER_RUN:
            dropped_line_count += 1
            continue
        if feed_source == TRANSCRIPTS_SOURCE:
            record = _validate_record_line(parsed_line, TRANSCRIPTS_SOURCE)
            if record is None:
                dropped_line_count += 1
            else:
                transcript_records.append(record)
                record_count += 1
        elif isinstance(feed_source, str) and feed_source in METRICS_SOURCES:
            record = _validate_record_line(parsed_line, feed_source)
            if record is None:
                dropped_line_count += 1
            else:
                metrics_records.append(record)
                record_count += 1
        else:
            dropped_line_count += 1
    return ParsedCollectionOutput(
        metrics_records=tuple(metrics_records),
        transcript_records=tuple(transcript_records),
        run_summary=run_summary,
        dropped_line_count=dropped_line_count,
    )
