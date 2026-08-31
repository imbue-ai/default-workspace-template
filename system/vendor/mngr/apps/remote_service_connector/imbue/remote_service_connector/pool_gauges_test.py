"""Tests for the pool-gauge sweep."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from inline_snapshot import snapshot

from imbue.remote_service_connector.pool_gauges import PoolGaugeRecord
from imbue.remote_service_connector.pool_gauges import PoolHostGroupCount
from imbue.remote_service_connector.pool_gauges import SlotRegionCapacity
from imbue.remote_service_connector.pool_gauges import compute_pool_gauge_records
from imbue.remote_service_connector.pool_gauges import run_pool_gauge_sweep
from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _user_headers


def _records_by_key(records: list[PoolGaugeRecord]) -> dict[tuple[str, ...], float]:
    return {(record.name, *sorted(record.tags.values())): record.value for record in records}


@contextmanager
def _capturing_emitted_metric_lines(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """Let caplog observe the metric records emitted inside the block.

    The metrics module's dedicated handler does not propagate; flip
    propagation so caplog's root handler observes the emitted lines, then
    restore the production setting.
    """
    metric_logger = logging.getLogger("imbue.modal_app_kit.metrics")
    original_propagate = metric_logger.propagate
    metric_logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="imbue.modal_app_kit.metrics"):
            yield
    finally:
        metric_logger.propagate = original_propagate


def test_compute_pool_gauge_records_zero_fills_the_status_branch_region_cross_product() -> None:
    group_counts = [
        PoolHostGroupCount(status="available", branch="minds-v0.4.3", region="US-EAST-VA", host_count=4),
        PoolHostGroupCount(status="leased", branch="minds-v0.4.2", region="US-WEST-OR", host_count=2),
    ]

    records = compute_pool_gauge_records(group_counts, [])
    host_count_records = [record for record in records if record.name == "pool_hosts_count"]
    by_key = _records_by_key(host_count_records)

    # 8 known statuses x 2 observed branches x 2 observed regions.
    assert len(host_count_records) == 32
    assert by_key[("pool_hosts_count", "US-EAST-VA", "available", "minds-v0.4.3")] == 4
    assert by_key[("pool_hosts_count", "US-WEST-OR", "leased", "minds-v0.4.2")] == 2
    # The drained combinations report explicit zeros rather than going silent.
    assert by_key[("pool_hosts_count", "US-WEST-OR", "available", "minds-v0.4.3")] == 0
    assert by_key[("pool_hosts_count", "US-EAST-VA", "available", "minds-v0.4.2")] == 0
    assert by_key[("pool_hosts_count", "US-EAST-VA", "minds-v0.4.3", "stopped")] == 0


def test_compute_pool_gauge_records_keeps_unknown_statuses_instead_of_dropping_them() -> None:
    group_counts = [
        PoolHostGroupCount(status="some_future_status", branch="minds-v0.4.3", region="US-EAST-VA", host_count=1),
    ]

    records = compute_pool_gauge_records(group_counts, [])
    by_key = _records_by_key(records)

    assert by_key[("pool_hosts_count", "US-EAST-VA", "minds-v0.4.3", "some_future_status")] == 1


def test_compute_pool_gauge_records_emits_slot_capacity_per_region_and_unions_regions() -> None:
    group_counts = [
        PoolHostGroupCount(status="available", branch="minds-v0.4.3", region="US-EAST-VA", host_count=1),
    ]
    capacities = [
        SlotRegionCapacity(region="US-WEST-OR", total_slots=16, used_slots=9),
    ]

    records = compute_pool_gauge_records(group_counts, capacities)
    by_key = _records_by_key(records)

    assert by_key[("pool_slots_total", "US-WEST-OR")] == 16
    assert by_key[("pool_slots_used", "US-WEST-OR")] == 9
    # The capacity-only region joins the host-count zero-fill domain, so a
    # region whose pool has fully drained still reports zeros.
    assert by_key[("pool_hosts_count", "US-WEST-OR", "available", "minds-v0.4.3")] == 0


def test_compute_pool_gauge_records_is_empty_for_an_empty_pool_and_no_servers() -> None:
    assert compute_pool_gauge_records([], []) == []


def test_run_pool_gauge_sweep_emits_records_and_the_heartbeat(caplog: pytest.LogCaptureFixture) -> None:
    group_counts = [
        PoolHostGroupCount(status="available", branch="minds-v0.4.3", region="US-EAST-VA", host_count=3),
    ]
    capacities = [SlotRegionCapacity(region="US-EAST-VA", total_slots=8, used_slots=5)]

    with _capturing_emitted_metric_lines(caplog):
        counters = run_pool_gauge_sweep(lambda: group_counts, lambda: capacities)

    assert counters == snapshot({"host_count_series": 8, "slot_regions": 1})
    emitted = [json.loads(record.getMessage()) for record in caplog.records]
    names = {line["name"] for line in emitted}
    assert names == {"pool_hosts_count", "pool_slots_total", "pool_slots_used", "pool_gauge_sweep_ok"}
    heartbeats = [line for line in emitted if line["name"] == "pool_gauge_sweep_ok"]
    assert len(heartbeats) == 1
    assert heartbeats[0]["value"] == 1


def test_run_pool_gauge_sweep_emits_only_the_heartbeat_for_an_empty_pool(caplog: pytest.LogCaptureFixture) -> None:
    with _capturing_emitted_metric_lines(caplog):
        counters = run_pool_gauge_sweep(lambda: [], lambda: [])

    assert counters == snapshot({"host_count_series": 0, "slot_regions": 0})
    emitted = [json.loads(record.getMessage()) for record in caplog.records]
    assert [line["name"] for line in emitted] == ["pool_gauge_sweep_ok"]


def test_admin_pool_gauge_sweep_route_requires_the_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)

    refused = client.post("/admin/sweep/pool-gauges", headers=_user_headers())
    allowed = client.post("/admin/sweep/pool-gauges", headers=_admin_key_headers())

    assert refused.status_code in (401, 403)
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "completed"
