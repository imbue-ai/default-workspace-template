"""The collector's HTTP API, on 127.0.0.1 only.

- ``GET /v1/totals?group_by=origin_id,model&since=24h[&until=...][&bucket=hour]``: cost and token totals.
- ``GET /v1/latency?group_by=model&since=24h[&until=...][&provider=anthropic][&billing_mode=API_KEY,UNKNOWN]``: call
  duration percentiles, optionally over only the calls whose fields take the given values.
- ``GET /v1/calls?provider=...&billing_mode=API_KEY,UNKNOWN&limit=50[&offset=N][&before=<event id>][&after=<sequence>]``:
  stored calls, newest first, optionally only those whose fields take one of the given values (also
  ``session_id``, ``run_id`` and ``turn_id``, e.g. ``?turn_id=<id>`` for one turn's calls), with the
  number of matching calls as ``total``.
- ``GET /v1/turns?provider=...&limit=20[&offset=N][&since=24h][&until=...]``: the turns of the calls the same
  filters keep (a call with no turn id is a turn of its own), latest first, each summing only those calls, with
  the number of turns as ``total``; ``since`` and ``until`` keep only calls that ended in that window; ``/v1/calls?turn_id=<id>`` (or ``?event_id=<id>``) lists one turn's calls.
- ``GET /v1/limits?since=7d``: the freshest subscription limit reading of every window.
- ``GET /v1/stream``: server-sent events, one ``model_call`` message per newly stored event.
- ``GET /health``: whether the collector is up, and whether it is still reading the event files.

Who may read usage is the share gateway's grants, not this API's: it listens
on 127.0.0.1 only, so a visitor reaches it only through an app the gateway
admitted them to (the Usage app).
"""

import json
import queue
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Final

from flask import Flask, Response, jsonify, request

from usage_collector.feed import UsageFeed
from usage_collector.status import IngestStatus
from usage_collector.store import (
    CALL_FILTER_DIMENSIONS,
    DEFAULT_CALLS_LIMIT,
    GROUPABLE_DIMENSIONS,
    TimeBucket,
    UsageStore,
    UsageStoreError,
)

DEFAULT_SINCE: Final[timedelta] = timedelta(hours=24)
# The longest window the harnesses report is a week, so an older reading says nothing current.
DEFAULT_LIMITS_SINCE: Final[timedelta] = timedelta(days=7)
_KEEPALIVE_SECONDS: Final[float] = 15.0
_DURATION = re.compile(r"^(\d+)([smhd])$")
_DURATION_UNITS: Final[dict[str, str]] = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}


class QueryParameterError(ValueError):
    """Raised for a totals query parameter that cannot be read."""


def parse_time(
    value: str | None, now: datetime, default: datetime | None
) -> datetime | None:
    """A duration before ``now`` (``90s``, ``30m``, ``24h``, ``7d``) or an ISO timestamp."""
    if value is None or not value.strip():
        return default
    match = _DURATION.match(value.strip())
    if match:
        return now - timedelta(**{_DURATION_UNITS[match.group(2)]: int(match.group(1))})
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as e:
        raise QueryParameterError(
            f"{value!r} is neither a duration like 24h nor an ISO timestamp"
        ) from e
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_bucket(value: str | None) -> TimeBucket | None:
    if value is None or not value.strip():
        return None
    for bucket in TimeBucket:
        if bucket.lower() == value.strip().lower():
            return bucket
    raise QueryParameterError(
        f"bucket must be one of {[bucket.lower() for bucket in TimeBucket]}"
    )


def parse_group_by(value: str | None) -> list[str]:
    return [field.strip() for field in (value or "").split(",") if field.strip()]


def parse_window(
    since_value: str | None, until_value: str | None, now: datetime
) -> tuple[datetime, datetime | None]:
    since = parse_time(since_value, now, None) or now - DEFAULT_SINCE
    return since, parse_time(until_value, now, None)


def parse_int(value: str | None, name: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as e:
        raise QueryParameterError(f"{name} must be a whole number") from e


def create_app(store: UsageStore, feed: UsageFeed, status: IngestStatus) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def _health() -> Response:
        return jsonify(status="ok", catching_up=status.is_catching_up)

    @app.get("/v1/totals")
    def _get_totals() -> Response | tuple[Response, int]:
        now = datetime.now(timezone.utc)
        group_by = parse_group_by(request.args.get("group_by"))
        try:
            since, until = parse_window(
                request.args.get("since"), request.args.get("until"), now
            )
            bucket = parse_bucket(request.args.get("bucket"))
            rows = store.totals(group_by, since, until, bucket)
        except (QueryParameterError, UsageStoreError) as e:
            return _error(str(e), 400)
        return jsonify(
            since=since.isoformat(),
            until=until.isoformat() if until is not None else None,
            group_by=group_by,
            bucket=str(bucket).lower() if bucket is not None else None,
            rows=[json.loads(row.model_dump_json()) for row in rows],
        )

    @app.get("/v1/latency")
    def _get_latency() -> Response | tuple[Response, int]:
        now = datetime.now(timezone.utc)
        group_by = parse_group_by(request.args.get("group_by"))
        try:
            since, until = parse_window(
                request.args.get("since"), request.args.get("until"), now
            )
            filters = {
                dimension: request.args[dimension].split(",")
                for dimension in GROUPABLE_DIMENSIONS
                if dimension in request.args
            }
            rows = store.latency(group_by, since, until, filters)
        except (QueryParameterError, UsageStoreError) as e:
            return _error(str(e), 400)
        return jsonify(
            since=since.isoformat(),
            until=until.isoformat() if until is not None else None,
            group_by=group_by,
            rows=[row.model_dump(mode="json") for row in rows],
        )

    @app.get("/v1/calls")
    def _get_calls() -> Response | tuple[Response, int]:
        filters = {
            dimension: request.args[dimension].split(",")
            for dimension in CALL_FILTER_DIMENSIONS
            if dimension in request.args
        }
        before = request.args.get("before") or None
        try:
            limit = parse_int(request.args.get("limit"), "limit")
            after = parse_int(request.args.get("after"), "after")
            offset = parse_int(request.args.get("offset"), "offset")
            calls = store.calls(
                filters,
                DEFAULT_CALLS_LIMIT if limit is None else limit,
                before=before,
                after=after,
                offset=0 if offset is None else offset,
            )
            total = store.count_calls(filters)
        except (QueryParameterError, UsageStoreError) as e:
            return _error(str(e), 400)
        return jsonify(
            calls=[call.model_dump(mode="json") for call in calls], total=total
        )

    @app.get("/v1/turns")
    def _get_turns() -> Response | tuple[Response, int]:
        filters = {
            dimension: request.args[dimension].split(",")
            for dimension in CALL_FILTER_DIMENSIONS
            if dimension in request.args
        }
        now = datetime.now(timezone.utc)
        try:
            limit = parse_int(request.args.get("limit"), "limit")
            offset = parse_int(request.args.get("offset"), "offset")
            since = parse_time(request.args.get("since"), now, None)
            until = parse_time(request.args.get("until"), now, None)
            turns = store.turns(
                filters,
                DEFAULT_CALLS_LIMIT if limit is None else limit,
                offset=0 if offset is None else offset,
                since=since,
                until=until,
            )
            total = store.count_turns(filters, since, until)
        except (QueryParameterError, UsageStoreError) as e:
            return _error(str(e), 400)
        return jsonify(
            turns=[turn.model_dump(mode="json") for turn in turns], total=total
        )

    @app.get("/v1/limits")
    def _get_limits() -> Response | tuple[Response, int]:
        now = datetime.now(timezone.utc)
        try:
            since = (
                parse_time(request.args.get("since"), now, None)
                or now - DEFAULT_LIMITS_SINCE
            )
        except QueryParameterError as e:
            return _error(str(e), 400)
        return jsonify(
            since=since.isoformat(),
            limits=[reading.model_dump(mode="json") for reading in store.limits(since)],
        )

    @app.get("/v1/stream")
    def _stream() -> Response:
        subscription = feed.subscribe()

        def _messages() -> Iterator[str]:
            try:
                yield ": connected\n\n"
                while subscription.is_open:
                    try:
                        message = subscription.events.get(timeout=_KEEPALIVE_SECONDS)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: model_call\ndata: {message}\n\n"
            finally:
                feed.unsubscribe(subscription)

        return Response(
            _messages(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _error(detail: str, status: int) -> tuple[Response, int]:
    return jsonify(detail=detail), status
