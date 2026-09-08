"""Tests for the lease-vs-record sweep."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from uuid import UUID

import pytest

from imbue.remote_service_connector.lease_records import LeaseRecordPair
from imbue.remote_service_connector.lease_records import LeaseRecordVerdictKind
from imbue.remote_service_connector.lease_records import classify_lease_record_pair
from imbue.remote_service_connector.lease_records import run_lease_record_sweep
from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _user_headers

_NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _pair(
    pool_status: str = "leased",
    record_state: str | None = "active",
    destroyed_at: datetime | None = None,
    released_at: datetime | None = None,
) -> LeaseRecordPair:
    return LeaseRecordPair(
        host_db_id="00000000-0000-0000-0000-0000000000e0",
        pool_status=pool_status,
        agent_id="agent-e0",
        host_id="host-e0",
        user_id_prefix=_USER_STUB_USER_ID_PREFIX,
        released_at=released_at,
        record_state=record_state,
        destroyed_at=destroyed_at,
    )


@pytest.mark.parametrize(
    ("pair", "expected_kind"),
    [
        (_pair(), LeaseRecordVerdictKind.CONSISTENT),
        (_pair(pool_status="stopped"), LeaseRecordVerdictKind.CONSISTENT),
        (_pair(record_state=None), LeaseRecordVerdictKind.NO_RECORD),
        (
            _pair(pool_status="removing", record_state=None, released_at=_NOW - timedelta(hours=7)),
            LeaseRecordVerdictKind.STALE_REMOVING,
        ),
        (
            _pair(pool_status="removing", record_state="active", released_at=_NOW - timedelta(days=2)),
            LeaseRecordVerdictKind.STALE_REMOVING,
        ),
        (
            _pair(pool_status="removing", record_state=None, released_at=_NOW - timedelta(minutes=2)),
            LeaseRecordVerdictKind.REMOVING_RECENT,
        ),
        (_pair(pool_status="removing", record_state=None, released_at=None), LeaseRecordVerdictKind.REMOVING_RECENT),
        (
            _pair(record_state="destroyed", destroyed_at=_NOW - timedelta(hours=1)),
            LeaseRecordVerdictKind.TOMBSTONED_RECENT,
        ),
        (_pair(record_state="destroyed", destroyed_at=_NOW - timedelta(hours=7)), LeaseRecordVerdictKind.TOMBSTONED),
        (_pair(record_state="destroyed", destroyed_at=None), LeaseRecordVerdictKind.TOMBSTONED_RECENT),
    ],
)
def test_classify_lease_record_pair(pair: LeaseRecordPair, expected_kind: LeaseRecordVerdictKind) -> None:
    verdict = classify_lease_record_pair(pair, _NOW, grace_seconds=6 * 3600)
    assert verdict.kind is expected_kind
    assert verdict.is_reapable is (
        expected_kind in (LeaseRecordVerdictKind.TOMBSTONED, LeaseRecordVerdictKind.STALE_REMOVING)
    )


def test_sweep_releases_leases_whose_tombstone_is_past_the_grace_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_workspace(
        suffix="f1",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        record_user_id=_USER_STUB_USER_ID_PREFIX,
        record_state="destroyed",
        destroyed_at=_NOW - timedelta(hours=7),
    )
    backend.add_leased_workspace(
        suffix="f2",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        record_user_id=_USER_STUB_USER_ID_PREFIX,
        record_state="destroyed",
        destroyed_at=_NOW - timedelta(minutes=5),
    )
    backend.add_leased_workspace(
        suffix="f3", leased_to_user=_USER_STUB_USER_ID_PREFIX, record_user_id=_USER_STUB_USER_ID_PREFIX
    )

    result = run_lease_record_sweep(grace_seconds=6 * 3600, now=_NOW)

    assert result["counts"] == {
        "consistent": 1,
        "tombstoned_recent": 1,
        "tombstoned": 1,
        "removing_recent": 0,
        "stale_removing": 0,
        "no_record": 0,
    }
    assert result["released"] == 1
    assert result["release_failed"] == 0
    assert sorted(row.agent_id for row in backend.pool_rows) == ["agent-f2", "agent-f3"]
    assert len(backend.slice_teardowns) == 1


def test_sweep_never_reaps_a_lease_without_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """No record is evidence of a bug, not a destroy intent: the row is reported and left alone."""
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000f4"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        agent_id="agent-f4",
        host_id_str="host-f4",
    )

    result = run_lease_record_sweep(grace_seconds=0, now=_NOW)

    assert result["counts"]["no_record"] == 1
    assert result["released"] == 0
    assert [row.agent_id for row in backend.pool_rows] == ["agent-f4"]
    assert backend.slice_teardowns == []


def test_sweep_redrives_rows_stuck_in_removing_past_the_grace_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000f5"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        agent_id="agent-f5",
        host_id_str="host-f5",
    )
    row.released_at = (_NOW - timedelta(hours=7)).isoformat()

    result = run_lease_record_sweep(grace_seconds=6 * 3600, now=_NOW)

    assert result["counts"]["stale_removing"] == 1
    assert result["released"] == 1
    assert backend.pool_rows == []


def test_sweep_leaves_a_release_that_is_still_in_flight_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh ``removing`` flip is a release its caller is still driving; racing it would double the teardown."""
    _client, backend = _make_pool_test_client(monkeypatch)
    row = backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000f8"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        agent_id="agent-f8",
        host_id_str="host-f8",
    )
    row.released_at = (_NOW - timedelta(seconds=30)).isoformat()

    result = run_lease_record_sweep(grace_seconds=6 * 3600, now=_NOW)

    assert result["counts"]["removing_recent"] == 1
    assert result["released"] == 0
    assert [pool_row.agent_id for pool_row in backend.pool_rows] == ["agent-f8"]
    assert backend.slice_teardowns == []


def test_sweep_confines_a_release_failure_to_its_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.slice_teardown_should_fail = True
    backend.add_leased_workspace(
        suffix="f6",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        record_user_id=_USER_STUB_USER_ID_PREFIX,
        record_state="destroyed",
        destroyed_at=_NOW - timedelta(days=1),
    )

    result = run_lease_record_sweep(grace_seconds=0, now=_NOW)

    assert result["released"] == 0
    assert result["release_failed"] == 1
    assert backend.pool_rows[0].status == "removing"


def test_sweep_dry_run_lists_candidates_without_releasing(monkeypatch: pytest.MonkeyPatch) -> None:
    _client, backend = _make_pool_test_client(monkeypatch)
    backend.add_leased_workspace(
        suffix="f7",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        record_user_id=_USER_STUB_USER_ID_PREFIX,
        record_state="destroyed",
        destroyed_at=_NOW - timedelta(days=1),
    )
    stuck = backend.add_removing_host(
        host_id=UUID("00000000-0000-0000-0000-0000000000f9"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        agent_id="agent-f9",
        host_id_str="host-f9",
    )
    stuck.released_at = (_NOW - timedelta(days=1)).isoformat()

    result = run_lease_record_sweep(grace_seconds=0, dry_run=True, now=_NOW)

    assert result["dry_run"] is True
    candidate_by_agent_id = {candidate["agent_id"]: candidate for candidate in result["candidates"]}
    assert sorted(candidate_by_agent_id) == ["agent-f7", "agent-f9"]
    # Each candidate carries the stamp that made it reapable.
    assert candidate_by_agent_id["agent-f7"]["kind"] == "tombstoned"
    assert candidate_by_agent_id["agent-f7"]["destroyed_at"] is not None
    assert candidate_by_agent_id["agent-f9"]["kind"] == "stale_removing"
    assert candidate_by_agent_id["agent-f9"]["destroyed_at"] is None
    assert candidate_by_agent_id["agent-f9"]["released_at"] is not None
    assert len(backend.pool_rows) == 2
    assert backend.slice_teardowns == []


def test_admin_lease_record_sweep_endpoint_requires_the_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)

    refused = client.post("/admin/sweep/lease-records", headers=_user_headers())
    allowed = client.post("/admin/sweep/lease-records?dry_run=1", headers=_admin_key_headers())

    assert refused.status_code in (401, 403)
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "completed"
    assert allowed.json()["result"]["dry_run"] is True
