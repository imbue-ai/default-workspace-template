from uuid import uuid4

import pytest

from imbue.minds_admin.primitives import derive_user_id_prefix
from imbue.minds_admin.slices.record_id_repair import MismatchSkipReason
from imbue.minds_admin.slices.record_id_repair import RecordIdRepairConflictError
from imbue.minds_admin.slices.record_id_repair import RecordIdRepairPlan
from imbue.minds_admin.slices.record_id_repair import RepairPoolRow
from imbue.minds_admin.slices.record_id_repair import RepairWorkspaceRecord
from imbue.minds_admin.slices.record_id_repair import _DELETE_ORPHAN_STUB_SQL
from imbue.minds_admin.slices.record_id_repair import _REPOINT_POOL_HOST_AGENT_ID_SQL
from imbue.minds_admin.slices.record_id_repair import _SELECT_LEASE_HOLDING_POOL_ROWS_SQL
from imbue.minds_admin.slices.record_id_repair import _SELECT_WORKSPACE_RECORDS_SQL
from imbue.minds_admin.slices.record_id_repair import apply_record_id_repair
from imbue.minds_admin.slices.record_id_repair import fetch_lease_holding_pool_rows
from imbue.minds_admin.slices.record_id_repair import fetch_workspace_records
from imbue.minds_admin.slices.record_id_repair import plan_record_id_repair
from imbue.minds_admin.slices.record_id_repair import run_record_id_repair
from imbue.minds_admin.slices.testing import RecordingConnection

_ALICE = "0123abcd-4567-89ef-0123-456789abcdef"
_BOB = "fedcba98-7654-3210-fedc-ba9876543210"


def _pool_row(
    host_id: str, agent_id: str, leased_to_user: str = derive_user_id_prefix(_ALICE), status: str = "leased"
) -> RepairPoolRow:
    return RepairPoolRow(
        host_db_id=str(uuid4()), host_id=host_id, agent_id=agent_id, leased_to_user=leased_to_user, status=status
    )


def _record(
    host_id: str,
    agent_id: str,
    user_id: str = _ALICE,
    state: str = "active",
    revision: int = 2,
    is_holding_encrypted_secrets: bool = True,
    backup_bucket: str | None = None,
    provider_kind: str = "imbue_cloud_alice-example-com",
) -> RepairWorkspaceRecord:
    return RepairWorkspaceRecord(
        user_id=user_id,
        agent_id=agent_id,
        host_id=host_id,
        state=state,
        revision=revision,
        is_holding_encrypted_secrets=is_holding_encrypted_secrets,
        backup_bucket=backup_bucket,
        provider_kind=provider_kind,
    )


def _stub(host_id: str, agent_id: str, user_id: str = _ALICE) -> RepairWorkspaceRecord:
    """The connector's lease-time stub: active, revision 1, no secrets, no bucket."""
    return _record(host_id, agent_id, user_id=user_id, revision=1, is_holding_encrypted_secrets=False)


def test_a_consistent_lease_is_left_alone() -> None:
    row = _pool_row("host-1", "agent-baked")
    plan = plan_record_id_repair([row], [_record("host-1", "agent-baked")])
    assert plan == RecordIdRepairPlan(repoints=(), skipped=(), orphan_stub_deletions=())
    assert plan.is_empty


def test_a_mismatched_lease_is_repointed_to_the_owners_record_for_the_same_host() -> None:
    row = _pool_row("host-1", "agent-baked")
    plan = plan_record_id_repair([row], [_record("host-1", "agent-rebuilt")])
    (repoint,) = plan.repoints
    assert repoint.host_db_id == row.host_db_id
    assert repoint.pool_agent_id == "agent-baked"
    assert repoint.record_agent_id == "agent-rebuilt"
    assert repoint.record_state == "active"
    assert plan.skipped == ()
    assert plan.orphan_stub_deletions == ()


def test_a_tombstoned_record_still_repoints_and_says_so() -> None:
    # The destroy intent belongs to the workspace; repointing lets the sweep act
    # on it, and the plan names the state so a dry run shows what will follow.
    plan = plan_record_id_repair(
        [_pool_row("host-1", "agent-baked")], [_record("host-1", "agent-rebuilt", state="destroyed")]
    )
    assert plan.repoints[0].record_state == "destroyed"


def test_another_users_record_for_the_same_host_never_matches() -> None:
    row = _pool_row("host-1", "agent-baked")
    plan = plan_record_id_repair([row], [_record("host-1", "agent-rebuilt", user_id=_BOB)])
    assert plan.repoints == ()
    (skipped,) = plan.skipped
    assert skipped.reason is MismatchSkipReason.NO_RECORD
    assert skipped.candidate_agent_ids == ()


def test_a_lease_with_no_record_at_all_is_reported_not_repaired() -> None:
    row = _pool_row("host-1", "agent-baked", status="stopped")
    plan = plan_record_id_repair([row], [])
    (skipped,) = plan.skipped
    assert skipped.host_db_id == row.host_db_id
    assert skipped.status == "stopped"
    assert skipped.reason is MismatchSkipReason.NO_RECORD


def test_several_candidate_records_for_one_host_are_ambiguous_and_skipped() -> None:
    row = _pool_row("host-1", "agent-baked")
    plan = plan_record_id_repair(
        [row], [_record("host-1", "agent-second"), _record("host-1", "agent-first", state="destroyed")]
    )
    assert plan.repoints == ()
    (skipped,) = plan.skipped
    assert skipped.reason is MismatchSkipReason.AMBIGUOUS
    assert skipped.candidate_agent_ids == ("agent-first", "agent-second")


def test_a_post_stub_slow_path_lease_is_repointed_and_its_stub_deleted_in_one_plan() -> None:
    # The lease-time stub under the baked id beside the desktop's record under
    # the rebuilt id: the stub is not a client's record, so the row is a
    # mismatch, and once repointed the stub is stranded.
    row = _pool_row("host-1", "agent-baked")
    records = [_stub("host-1", "agent-baked"), _record("host-1", "agent-rebuilt")]
    plan = plan_record_id_repair([row], records)
    (repoint,) = plan.repoints
    assert repoint.pool_agent_id == "agent-baked"
    assert repoint.record_agent_id == "agent-rebuilt"
    assert plan.skipped == ()
    (deletion,) = plan.orphan_stub_deletions
    assert deletion.agent_id == "agent-baked"
    assert deletion.host_id == "host-1"


def test_a_fresh_lease_whose_only_record_is_its_stub_is_consistent() -> None:
    # The desktop has not pushed yet (or the fast path adopted the baked agent
    # and its first push will land on this very row): nothing to repair.
    row = _pool_row("host-1", "agent-baked")
    plan = plan_record_id_repair([row], [_stub("host-1", "agent-baked")])
    assert plan.is_empty
    assert plan.skipped == ()


def test_a_stub_under_another_id_never_claims_the_host_but_is_named_for_the_operator() -> None:
    row = _pool_row("host-1", "agent-hand-edited")
    plan = plan_record_id_repair([row], [_stub("host-1", "agent-baked")])
    assert plan.repoints == ()
    (skipped,) = plan.skipped
    assert skipped.reason is MismatchSkipReason.UNCONFIRMED_RECORD
    assert skipped.candidate_agent_ids == ("agent-baked",)
    assert plan.orphan_stub_deletions == ()


def test_a_pre_stub_lease_whose_only_record_is_stub_shaped_is_unconfirmed_not_record_less() -> None:
    # A lease from before the connector wrote stubs has no record under its own
    # id; the desktop's record under the rebuilt id can still have the stub's
    # shape, and the operator needs its id to resolve the row by hand.
    row = _pool_row("host-1", "agent-baked")
    plan = plan_record_id_repair([row], [_stub("host-1", "agent-rebuilt")])
    assert plan.repoints == ()
    (skipped,) = plan.skipped
    assert skipped.reason is MismatchSkipReason.UNCONFIRMED_RECORD
    assert skipped.candidate_agent_ids == ("agent-rebuilt",)
    assert plan.orphan_stub_deletions == ()


def test_a_stub_shaped_client_record_is_reported_and_never_deleted() -> None:
    # The desktop's record under the rebuilt id can have the stub's shape (a
    # first push at revision 1, secrets stripped without a master password, no
    # backup bucket yet): the repair cannot tell it from the lease's stub, so
    # it reports the row and leaves every record of the host in place.
    row = _pool_row("host-1", "agent-baked")
    plan = plan_record_id_repair([row], [_stub("host-1", "agent-baked"), _stub("host-1", "agent-rebuilt")])
    assert plan.repoints == ()
    (skipped,) = plan.skipped
    assert skipped.host_db_id == row.host_db_id
    assert skipped.reason is MismatchSkipReason.UNCONFIRMED_RECORD
    assert skipped.candidate_agent_ids == ("agent-rebuilt",)
    assert plan.orphan_stub_deletions == ()


def test_an_orphaned_stub_is_deleted_once_its_lease_points_elsewhere() -> None:
    # A row already repointed by an earlier run (or by hand) leaves the stub
    # with no lease by agent id while its host id is held under another id.
    row = _pool_row("host-1", "agent-rebuilt")
    records = [_stub("host-1", "agent-baked"), _record("host-1", "agent-rebuilt")]
    plan = plan_record_id_repair([row], records)
    assert plan.repoints == ()
    (deletion,) = plan.orphan_stub_deletions
    assert deletion.user_id == _ALICE
    assert deletion.agent_id == "agent-baked"
    assert deletion.host_id == "host-1"


def test_nothing_of_an_ambiguous_host_is_deleted() -> None:
    # Two client-written records name the host: the operator resolves it, and
    # the stub (an orphan by the agent-id rule alone) stays until they do.
    row = _pool_row("host-1", "agent-hand-edited")
    records = [
        _stub("host-1", "agent-baked"),
        _record("host-1", "agent-rebuilt"),
        _record("host-1", "agent-restored", state="destroyed"),
    ]
    plan = plan_record_id_repair([row], records)
    assert plan.repoints == ()
    assert plan.skipped[0].reason is MismatchSkipReason.AMBIGUOUS
    assert plan.orphan_stub_deletions == ()


@pytest.mark.parametrize(
    "record",
    [
        _record("host-1", "agent-baked", revision=1, is_holding_encrypted_secrets=True),
        _record("host-1", "agent-baked", revision=1, is_holding_encrypted_secrets=False, backup_bucket="b"),
        _record("host-1", "agent-baked", revision=3, is_holding_encrypted_secrets=False),
        _record("host-1", "agent-baked", revision=1, is_holding_encrypted_secrets=False, state="destroyed"),
        _record("host-1", "agent-baked", revision=1, is_holding_encrypted_secrets=False, provider_kind="docker"),
    ],
)
def test_only_an_untouched_cloud_stub_shape_is_ever_deleted(record: RepairWorkspaceRecord) -> None:
    row = _pool_row("host-1", "agent-rebuilt")
    plan = plan_record_id_repair([row], [record, _record("host-1", "agent-rebuilt")])
    assert plan.orphan_stub_deletions == ()


def test_a_stub_whose_host_no_lease_holds_is_not_an_orphan_of_this_repair() -> None:
    # No lease names the host at all: the record may be a client's own row for
    # a released workspace, which the retention paths own -- not this repair.
    plan = plan_record_id_repair([], [_stub("host-1", "agent-baked")])
    assert plan.orphan_stub_deletions == ()


def test_the_pool_row_query_selects_every_lease_holding_status_with_an_owner_and_an_agent() -> None:
    for status in ("leased", "stopping", "stopped", "starting", "crashed", "removing"):
        assert f"'{status}'" in _SELECT_LEASE_HOLDING_POOL_ROWS_SQL
    assert "'available'" not in _SELECT_LEASE_HOLDING_POOL_ROWS_SQL
    assert "agent_id IS NOT NULL AND leased_to_user IS NOT NULL" in _SELECT_LEASE_HOLDING_POOL_ROWS_SQL
    conn = RecordingConnection([("row-1", "host-1", "agent-1", "0123abcd456789ef", "stopped")], rowcount=1)
    (row,) = fetch_lease_holding_pool_rows(conn)
    assert row == RepairPoolRow(
        host_db_id="row-1", host_id="host-1", agent_id="agent-1", leased_to_user="0123abcd456789ef", status="stopped"
    )


def test_the_record_query_reads_secrets_presence_not_the_blob() -> None:
    assert "encrypted_secrets IS NOT NULL" in _SELECT_WORKSPACE_RECORDS_SQL
    conn = RecordingConnection([(_ALICE, "agent-1", "host-1", "active", 1, False, None, None)], rowcount=1)
    (record,) = fetch_workspace_records(conn)
    assert record.is_untouched_cloud_stub is False
    assert record.provider_kind == ""
    assert record.user_id_prefix == "0123abcd456789ef"


def test_apply_runs_every_write_as_a_compare_and_swap_then_commits_once() -> None:
    row = _pool_row("host-1", "agent-baked")
    stranded = _pool_row("host-2", "agent-rebuilt-2")
    plan = plan_record_id_repair(
        [row, stranded],
        [_record("host-1", "agent-rebuilt"), _stub("host-2", "agent-baked-2"), _record("host-2", "agent-rebuilt-2")],
    )
    assert len(plan.repoints) == 1 and len(plan.orphan_stub_deletions) == 1
    conn = RecordingConnection([], rowcount=1)

    apply_record_id_repair(conn, plan)

    assert conn.recording_cursor.executed == [
        (_REPOINT_POOL_HOST_AGENT_ID_SQL, ("agent-rebuilt", row.host_db_id, "agent-baked")),
        (_DELETE_ORPHAN_STUB_SQL, (_ALICE, "agent-baked-2")),
    ]
    assert "WHERE id = %s AND agent_id = %s" in _REPOINT_POOL_HOST_AGENT_ID_SQL
    for guard in ("state = 'active'", "revision = 1", "encrypted_secrets IS NULL", "backup_bucket IS NULL"):
        assert guard in _DELETE_ORPHAN_STUB_SQL
    assert conn.commit_count == 1
    assert conn.rollback_count == 0


def test_apply_rolls_back_and_raises_when_a_row_changed_since_the_plan() -> None:
    plan = plan_record_id_repair([_pool_row("host-1", "agent-baked")], [_record("host-1", "agent-rebuilt")])
    conn = RecordingConnection([], rowcount=0)

    with pytest.raises(RecordIdRepairConflictError, match="no longer carries agent id agent-baked"):
        apply_record_id_repair(conn, plan)

    assert conn.commit_count == 0
    assert conn.rollback_count == 1


def test_apply_with_an_empty_plan_writes_nothing_and_commits_once() -> None:
    conn = RecordingConnection([], rowcount=0)
    apply_record_id_repair(conn, plan_record_id_repair([], []))
    assert conn.recording_cursor.executed == []
    assert conn.commit_count == 1


def _drifted_lease_connection(rowcount: int) -> RecordingConnection:
    """A pool with one slow-path lease under the baked id beside the desktop's record under the rebuilt id."""
    return RecordingConnection(
        [],
        rowcount=rowcount,
        rows_by_sql={
            _SELECT_LEASE_HOLDING_POOL_ROWS_SQL: [
                ("row-1", "host-1", "agent-baked", derive_user_id_prefix(_ALICE), "leased")
            ],
            _SELECT_WORKSPACE_RECORDS_SQL: [
                (_ALICE, "agent-baked", "host-1", "active", 1, False, None, "imbue_cloud_alice-example-com"),
                (_ALICE, "agent-rebuilt", "host-1", "active", 2, True, None, "imbue_cloud_alice-example-com"),
            ],
        },
    )


def test_a_dry_run_plans_from_both_tables_and_writes_nothing() -> None:
    conn = _drifted_lease_connection(rowcount=0)

    report = run_record_id_repair(conn, is_execute=False)

    assert report.is_applied is False
    assert (report.repointed_count, report.deleted_stub_count, report.skipped_count) == (1, 1, 0)
    assert report.plan.repoints[0].record_agent_id == "agent-rebuilt"
    assert [sql for sql, _params in conn.recording_cursor.executed] == [
        _SELECT_LEASE_HOLDING_POOL_ROWS_SQL,
        _SELECT_WORKSPACE_RECORDS_SQL,
    ]
    assert conn.commit_count == 0


def test_an_execute_run_applies_the_plan_it_computed() -> None:
    conn = _drifted_lease_connection(rowcount=1)

    report = run_record_id_repair(conn, is_execute=True)

    assert report.is_applied is True
    assert conn.recording_cursor.executed[2:] == [
        (_REPOINT_POOL_HOST_AGENT_ID_SQL, ("agent-rebuilt", "row-1", "agent-baked")),
        (_DELETE_ORPHAN_STUB_SQL, (_ALICE, "agent-baked")),
    ]
    assert conn.commit_count == 1


def test_an_execute_run_with_nothing_to_repair_commits_nothing() -> None:
    conn = RecordingConnection([], rowcount=0)

    report = run_record_id_repair(conn, is_execute=True)

    assert report.is_applied is False
    assert report.plan.is_empty
    assert len(conn.recording_cursor.executed) == 2
    assert conn.commit_count == 0
