"""The pool-gauge sweep: emit the pool's composition and slot capacity as metric records.

The dashboards that watch a release roll out need two numbers the request
logs cannot supply: how many baked hosts sit unleased at each template
branch in each region, and how many empty bare-metal slots each region has
left. Both live only in the connector's DB, so this sweep periodically
reads them and emits them as ``metric`` log records (``modal_app_kit``
metrics convention), which Modal's OTEL integration ships into the tier's
OpenObserve ``modal_logs`` stream for charting.

Gauge semantics over log lines need explicit zeros: a last-value panel
keeps showing the previous reading when a series simply stops being
emitted, which is exactly wrong at the moment a pool drains. So every pass
emits the full cross-product of known statuses x observed branches x known
regions (0 where nothing matches), plus a ``pool_gauge_sweep_ok`` heartbeat
so a silent sweep is itself visible.

Slot occupancy is the connector's own accounting: a row occupies a slot
exactly while its ``bare_metal_server_id`` is set (the stop flow nulls the
placement columns when the slot frees). This deliberately ignores what sits
on the boxes outside this env's DB (other envs' slices on shared dev boxes,
retention-window leftovers) -- the bake's on-box occupancy check remains
the capacity guard; these gauges are the dashboard view.
"""

from collections.abc import Callable
from collections.abc import Sequence
from typing import Final

from fastapi import APIRouter
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.http_api import handle_endpoint_errors

router = APIRouter()

# Every pool_hosts lifecycle status (lease flow + stop/start migration 024 +
# the lease-time quarantine). Unknown statuses that appear in the DB are still
# emitted (nothing is dropped); this fixed set is the zero-fill domain, so a
# status that empties out keeps reporting 0 instead of going silent.
POOL_HOST_STATUSES: Final[tuple[str, ...]] = (
    "available",
    "leased",
    "stopping",
    "stopped",
    "starting",
    "crashed",
    "removing",
    "unreachable",
)

# Metric record names (the ``name`` field dashboards filter on).
POOL_HOSTS_COUNT_METRIC: Final[str] = "pool_hosts_count"
POOL_SLOTS_TOTAL_METRIC: Final[str] = "pool_slots_total"
POOL_SLOTS_USED_METRIC: Final[str] = "pool_slots_used"
POOL_GAUGE_HEARTBEAT_METRIC: Final[str] = "pool_gauge_sweep_ok"


class PoolHostGroupCount(BaseModel):
    """One (status, branch, region) group of pool rows and its size."""

    status: str = Field(description="The rows' lifecycle status")
    branch: str = Field(description="The rows' baked template branch (attributes repo_branch_or_tag; '' when unset)")
    region: str = Field(description="The rows' lease-region label ('' when unset)")
    host_count: int = Field(description="Number of pool rows in the group")


class SlotRegionCapacity(BaseModel):
    """One region's bare-metal slot capacity and occupancy."""

    region: str = Field(description="The OVH datacenter region label")
    total_slots: int = Field(description="Sum of slot_count over the region's ready boxes")
    used_slots: int = Field(description="Pool rows currently placed on the region's ready boxes")


class PoolGaugeRecord(BaseModel):
    """One metric record the sweep emits."""

    name: str = Field(description="Metric record name")
    value: float = Field(description="Gauge value")
    tags: dict[str, str] = Field(description="Low-cardinality series tags")


def _list_pool_host_group_counts() -> list[PoolHostGroupCount]:
    """Every (status, branch, region) group present in pool_hosts, with its row count."""
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, coalesce(attributes->>'repo_branch_or_tag', ''), coalesce(region, ''), count(*) "
                "FROM pool_hosts GROUP BY 1, 2, 3"
            )
            rows = cur.fetchall()
    return [
        PoolHostGroupCount(
            status=str(row[0]),
            branch=str(row[1]),
            region=str(row[2]),
            host_count=int(row[3]),
        )
        for row in rows
    ]


def _list_slot_region_capacities() -> list[SlotRegionCapacity]:
    """Per-region slot capacity over ready boxes, with occupancy from placed pool rows.

    Regions whose boxes are all non-ready still get a row (total 0), so a
    region that loses its last ready box reports zero capacity instead of
    disappearing from the gauge.
    """
    with db.pooled_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.region, "
                " coalesce(sum(CASE WHEN s.status = 'ready' THEN s.slot_count ELSE 0 END), 0), "
                " coalesce(sum(CASE WHEN s.status = 'ready' THEN coalesce(placed.placed_count, 0) ELSE 0 END), 0) "
                "FROM bare_metal_servers s "
                "LEFT JOIN ("
                " SELECT bare_metal_server_id, count(*) AS placed_count FROM pool_hosts "
                " WHERE bare_metal_server_id IS NOT NULL GROUP BY bare_metal_server_id"
                ") placed ON placed.bare_metal_server_id = s.id "
                "GROUP BY s.region"
            )
            rows = cur.fetchall()
    return [
        SlotRegionCapacity(
            region=str(row[0]),
            total_slots=int(row[1]),
            used_slots=int(row[2]),
        )
        for row in rows
    ]


def compute_pool_gauge_records(
    group_counts: Sequence[PoolHostGroupCount],
    capacities: Sequence[SlotRegionCapacity],
) -> list[PoolGaugeRecord]:
    """Turn the two DB reads into the pass's zero-filled gauge records (pure).

    Host counts are zero-filled over known statuses x observed branches x
    known regions (branch/region domains come from what the DB actually
    holds -- an empty pool has no series to keep truthful). Statuses outside
    the known set are emitted as observed, never dropped.
    """
    count_by_group = {(group.status, group.branch, group.region): group.host_count for group in group_counts}
    branches = sorted({group.branch for group in group_counts})
    regions = sorted({group.region for group in group_counts} | {capacity.region for capacity in capacities})
    statuses = list(POOL_HOST_STATUSES) + sorted({group.status for group in group_counts} - set(POOL_HOST_STATUSES))

    records: list[PoolGaugeRecord] = []
    for status in statuses:
        for branch in branches:
            for region in regions:
                records.append(
                    PoolGaugeRecord(
                        name=POOL_HOSTS_COUNT_METRIC,
                        value=float(count_by_group.get((status, branch, region), 0)),
                        tags={"status": status, "branch": branch, "region": region},
                    )
                )
    for capacity in capacities:
        records.append(
            PoolGaugeRecord(
                name=POOL_SLOTS_TOTAL_METRIC,
                value=float(capacity.total_slots),
                tags={"region": capacity.region},
            )
        )
        records.append(
            PoolGaugeRecord(
                name=POOL_SLOTS_USED_METRIC,
                value=float(capacity.used_slots),
                tags={"region": capacity.region},
            )
        )
    return records


def run_pool_gauge_sweep(
    list_group_counts: Callable[[], list[PoolHostGroupCount]],
    list_capacities: Callable[[], list[SlotRegionCapacity]],
) -> dict[str, int]:
    """One sweep pass: read the pool, emit every gauge record plus the heartbeat."""
    records = compute_pool_gauge_records(list_group_counts(), list_capacities())
    for record in records:
        emit_metric(record.name, record.value, record.tags)
    # The heartbeat proves the sweep ran even when the pool is empty (no
    # series at all); a staleness panel/alert watches this record.
    emit_metric(POOL_GAUGE_HEARTBEAT_METRIC, 1, {})
    return {
        "host_count_series": sum(1 for record in records if record.name == POOL_HOSTS_COUNT_METRIC),
        "slot_regions": sum(1 for record in records if record.name == POOL_SLOTS_TOTAL_METRIC),
    }


def run_pool_gauge_sweep_from_db() -> dict[str, int]:
    """The production sweep body: the DB-backed listers wired into one pass."""
    return run_pool_gauge_sweep(_list_pool_host_group_counts, _list_slot_region_capacities)


@router.post("/admin/sweep/pool-gauges")
def admin_run_pool_gauge_sweep(request: Request) -> dict[str, object]:
    """Run one pool-gauge sweep pass on demand (operator tool + deployment tests).

    Authenticated by the fixed operator admin key (``MINDS_ADMIN_KEY``).
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        result = run_pool_gauge_sweep_from_db()
        return {"status": "completed", "result": result}
