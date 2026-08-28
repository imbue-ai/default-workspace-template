"""The lease-vs-record sweep: reap leases whose destroy intent is on record, alarm on the rest.

A pool lease (``pool_hosts``) and a workspace record (``workspace_records``)
are two views of one cloud workspace. The lease grant writes the record stub
and the release retires it (see ``sync.py``), so in steady state every
lease-holding row has an ACTIVE record and every tombstoned record's row is
already gone. This module acts on the drift that survives:

* a lease whose record is a **tombstone** older than the grace window -- the
  user's destroy intent is durable and the release evidently failed -- is
  released (artifacts deleted, slice VM destroyed, row dropped);
* a row still **``removing``** past the same window -- a release that failed
  partway (the flip is the same kind of durable destroy intent) -- is
  re-driven the same way; a fresh flip is a release in flight and is left to
  its caller;
* a lease that is not ``removing`` and has **no record at all** is
  impossible through legitimate paths (only a release in progress deletes a
  record, and it flips the row first), so it is reported (one warning per
  pass, plus a metric) and never auto-reaped: destroying a VM on evidence of
  a bug is how you delete something a user still wanted.

Every pass emits per-kind counts as metric records so the drift is a
chartable number.
"""

import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
from typing import Any
from typing import Final

from fastapi import APIRouter
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.hosts as hosts_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.sync import LEASE_HOLDING_STATUSES_SQL
from imbue.remote_service_connector.sync import user_id_prefix_sql

logger = logging.getLogger(__name__)

router = APIRouter()

# How old a destroy intent -- a record's tombstone, or a row's ``removing``
# flip -- must be before the sweep reaps the lease. A healthy destroy releases
# the lease *before* the desktop tombstones the record, so a tombstone beside a
# live row already means a release failed; the window is headroom for a destroy
# that is mid-retry (or a release still in flight), not a normal-path delay.
LEASE_RECORD_SWEEP_GRACE_SECONDS: Final[float] = 6.0 * 60.0 * 60.0

# How many releases one pass runs; the rest wait for the next pass. Sized so
# that even a pass whose every box is unreachable (a 30s SSH connect timeout
# per release, on top of the S3 prefix delete and the DB round-trips) finishes
# well inside the cron's 15-minute Modal timeout instead of being killed
# mid-release.
_SWEEP_RELEASE_BUDGET_PER_PASS: Final[int] = 10


class LeaseRecordPair(BaseModel):
    """One lease-holding pool row joined with its owner's record for the same workspace."""

    host_db_id: str = Field(description="The pool_hosts row id")
    pool_status: str = Field(description="The row's lifecycle status")
    agent_id: str | None = Field(description="The workspace id (the pre-baked services agent id)")
    host_id: str | None = Field(description="The mngr host id")
    user_id_prefix: str | None = Field(description="The leasing user's 16-hex prefix")
    released_at: datetime | None = Field(description="When the row was flipped to ``removing``, for such rows")
    record_state: str | None = Field(description="The record's state, None when the workspace has no record")
    destroyed_at: datetime | None = Field(description="The record's tombstone stamp, when tombstoned")


def _list_lease_record_pairs() -> list[LeaseRecordPair]:
    """Every lease-holding pool row, LEFT JOINed with the owner's record for the same workspace."""
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.id, p.status, p.agent_id, p.host_id, p.leased_to_user, p.released_at, "
                "r.state, r.destroyed_at "
                "FROM pool_hosts p LEFT JOIN workspace_records r "
                f"ON r.agent_id = p.agent_id AND {user_id_prefix_sql('r.user_id')} = p.leased_to_user "
                f"WHERE p.status IN ({LEASE_HOLDING_STATUSES_SQL}) "
                "ORDER BY p.leased_at"
            )
            rows = cur.fetchall()
    return [
        LeaseRecordPair(
            host_db_id=str(row[0]),
            pool_status=str(row[1]),
            agent_id=row[2],
            host_id=row[3],
            user_id_prefix=row[4],
            released_at=row[5],
            record_state=row[6],
            destroyed_at=row[7],
        )
        for row in rows
    ]


class LeaseRecordVerdictKind(str, Enum):
    """What the sweep does about one lease-holding row (lowercase: these are metric tags and JSON output)."""

    # An ACTIVE record: the two views agree.
    CONSISTENT = "consistent"
    # A tombstone inside the grace window: a destroy that may still be mid-retry.
    TOMBSTONED_RECENT = "tombstoned_recent"
    # A tombstone past the grace window: reap.
    TOMBSTONED = "tombstoned"
    # A ``removing`` flip inside the grace window: a release still in flight.
    REMOVING_RECENT = "removing_recent"
    # A ``removing`` flip past the grace window: reap.
    STALE_REMOVING = "stale_removing"
    # No record at all: alarm, never reap.
    NO_RECORD = "no_record"


_REAPABLE_VERDICT_KINDS: Final[frozenset[LeaseRecordVerdictKind]] = frozenset(
    (LeaseRecordVerdictKind.TOMBSTONED, LeaseRecordVerdictKind.STALE_REMOVING)
)


class LeaseRecordVerdict(BaseModel):
    """What the sweep concluded about one lease-holding row."""

    kind: LeaseRecordVerdictKind = Field(description="The sweep's action for the row")
    pair: LeaseRecordPair = Field(description="The row + record the verdict is about")

    @property
    def is_reapable(self) -> bool:
        return self.kind in _REAPABLE_VERDICT_KINDS


def classify_lease_record_pair(pair: LeaseRecordPair, now: datetime, grace_seconds: float) -> LeaseRecordVerdict:
    """Decide the sweep's action for one row (pure)."""
    intent_cutoff = now - timedelta(seconds=grace_seconds)
    if pair.pool_status == "removing":
        if pair.released_at is not None and pair.released_at <= intent_cutoff:
            return LeaseRecordVerdict(kind=LeaseRecordVerdictKind.STALE_REMOVING, pair=pair)
        return LeaseRecordVerdict(kind=LeaseRecordVerdictKind.REMOVING_RECENT, pair=pair)
    if pair.record_state is None:
        return LeaseRecordVerdict(kind=LeaseRecordVerdictKind.NO_RECORD, pair=pair)
    if pair.record_state != "destroyed":
        return LeaseRecordVerdict(kind=LeaseRecordVerdictKind.CONSISTENT, pair=pair)
    if pair.destroyed_at is not None and pair.destroyed_at <= intent_cutoff:
        return LeaseRecordVerdict(kind=LeaseRecordVerdictKind.TOMBSTONED, pair=pair)
    return LeaseRecordVerdict(kind=LeaseRecordVerdictKind.TOMBSTONED_RECENT, pair=pair)


def run_lease_record_sweep(
    *,
    grace_seconds: float = LEASE_RECORD_SWEEP_GRACE_SECONDS,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One sweep pass: classify every lease-holding row, reap the reapable ones, alarm on the rest.

    ``dry_run`` reports the verdicts without releasing anything. Releases are
    bounded per pass and each row's failure is confined to that row (logged,
    counted, left ``removing`` for the next pass).
    """
    current_time = now if now is not None else datetime.now(timezone.utc)
    verdicts = [classify_lease_record_pair(pair, current_time, grace_seconds) for pair in _list_lease_record_pairs()]
    counts = {kind.value: sum(1 for verdict in verdicts if verdict.kind == kind) for kind in LeaseRecordVerdictKind}
    for kind_value, count in counts.items():
        emit_metric("lease_record_drift", count, {"kind": kind_value})

    # Outside a release in progress (a ``removing`` row, classified above), a
    # lease without a record cannot arise through the lease/release paths, so
    # it is evidence of a bug (or a hand-edited table), never a destroy
    # intent: one warning per pass names every such row and nothing is reaped.
    orphan_ids = [verdict.pair.host_db_id for verdict in verdicts if verdict.kind == LeaseRecordVerdictKind.NO_RECORD]
    if orphan_ids:
        logger.warning(
            "Lease-record sweep found %d lease(s) with no workspace record (never auto-reaped): %s",
            len(orphan_ids),
            ", ".join(orphan_ids),
        )

    reapable = [verdict for verdict in verdicts if verdict.is_reapable]
    if dry_run:
        return _render_dry_run_report(reapable, counts, grace_seconds)
    outcome = _release_reapable_leases(reapable)
    return {
        "counts": counts,
        "released": outcome.released_count,
        "release_failed": outcome.release_failed_count,
        "deferred": outcome.deferred_count,
    }


def _render_dry_run_report(
    reapable: list[LeaseRecordVerdict], counts: dict[str, int], grace_seconds: float
) -> dict[str, Any]:
    return {
        "dry_run": True,
        "grace_seconds": grace_seconds,
        "counts": counts,
        "candidates": [
            {
                "kind": verdict.kind.value,
                "host_db_id": verdict.pair.host_db_id,
                "pool_status": verdict.pair.pool_status,
                "agent_id": verdict.pair.agent_id,
                "destroyed_at": str(verdict.pair.destroyed_at) if verdict.pair.destroyed_at is not None else None,
                "released_at": str(verdict.pair.released_at) if verdict.pair.released_at is not None else None,
            }
            for verdict in reapable
        ],
    }


class _SweepReleaseOutcome(BaseModel):
    """How one sweep pass's bounded release loop went."""

    released_count: int = Field(description="Leases released end to end this pass")
    release_failed_count: int = Field(
        description="Releases that failed (their rows stay ``removing`` for the next pass)"
    )
    deferred_count: int = Field(description="Reapable leases beyond this pass's budget, left for the next pass")


def _release_reapable_leases(reapable: list[LeaseRecordVerdict]) -> _SweepReleaseOutcome:
    """Release up to the per-pass budget of reapable leases, confining each failure to its row."""
    released_count = 0
    release_failed_count = 0
    for verdict in reapable[:_SWEEP_RELEASE_BUDGET_PER_PASS]:
        try:
            outcome = hosts_module.release_pool_host_row(verdict.pair.host_db_id)
        except hosts_module.RELEASE_FAILURE_ERROR_TYPES as exc:
            release_failed_count += 1
            logger.warning(
                "Lease-record sweep could not release %s (%s): %s", verdict.pair.host_db_id, verdict.kind.value, exc
            )
            continue
        released_count += 1
        logger.info("Lease-record sweep released %s (%s): %s", verdict.pair.host_db_id, verdict.kind.value, outcome)
    deferred_count = max(0, len(reapable) - _SWEEP_RELEASE_BUDGET_PER_PASS)
    if deferred_count > 0:
        logger.info(
            "Lease-record sweep deferred %d reapable lease(s) to the next pass (budget %d)",
            deferred_count,
            _SWEEP_RELEASE_BUDGET_PER_PASS,
        )
    return _SweepReleaseOutcome(
        released_count=released_count,
        release_failed_count=release_failed_count,
        deferred_count=deferred_count,
    )


@router.post("/admin/sweep/lease-records")
def admin_run_lease_record_sweep(
    request: Request, dry_run: bool = False, grace_seconds: float | None = None
) -> dict[str, object]:
    """Run one lease-vs-record sweep pass on demand (operator tool + deployment tests).

    Authenticated by the fixed operator admin key (``MINDS_ADMIN_KEY``).
    ``dry_run=1`` reports the verdicts and reap candidates without releasing
    anything; ``grace_seconds`` overrides the destroy-intent grace window
    (admin-only, e.g. ``0`` to reap a fresh tombstone in a deployment test).
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        result = run_lease_record_sweep(
            grace_seconds=grace_seconds if grace_seconds is not None else LEASE_RECORD_SWEEP_GRACE_SECONDS,
            dry_run=dry_run,
        )
        return {"status": "completed", "result": result}
