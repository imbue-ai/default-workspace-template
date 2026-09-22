"""Operator repair re-aligning pool leases whose services agent id drifted from their workspace record.

A pool lease (``pool_hosts``) and a workspace record (``workspace_records``)
are two views of one cloud workspace, joined on the workspace id -- the
services agent id -- and the owner's user-id prefix. Before the imbue_cloud
plugin pinned the slow path's rebuilt container to the lease's agent id, a
slow-path create minted a fresh id, so the desktop's record carries that id
while the pool row keeps the baked one: the connector's lease-record sweep
then reports the lease as record-less, and a lease-time record stub under the
baked id sits beside the desktop's record as a duplicate list entry.

The repair plans over a snapshot of both tables (pure), then applies it in
one transaction. A row whose agent id names no client-written record of its
owner (no record at all, or only the untouched lease-time stub) is repointed
to the owner's client-written record naming the same host id when exactly one
exists; zero candidates leave a stub-only row alone (a fresh lease the desktop
has not pushed yet) and report a record-less one, and several candidates are
reported for the operator. So is a row whose only other records naming its
host have the stub shape: a client's record pushed without a master password
and without a backup bucket is indistinguishable from a stub, and nothing of
such a host is deleted. The stubs the repoints orphan -- never written by a
client, no longer matching any lease by agent id while their host id belongs
to a lease under another id -- are deleted. Every write is a compare-and-swap
on the state the plan saw, so a concurrent change fails the run instead of
being overwritten. Safe to re-run.

CLEANUP: delete this module, ``workspaces repair-record-ids``, and their tests
after a final run once no minds client older than the release carrying the
plugin's pinned-id slow path appears in the connector access log's
``imbue_client`` field; nothing creates mismatched rows after that.
"""

from collections.abc import Sequence
from enum import auto
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.minds_admin.primitives import derive_user_id_prefix

# Every pool_hosts status under which a workspace still holds its lease.
# Mirrors the connector's ``LEASE_HOLDING_POOL_STATUSES`` (duplicated, not
# imported: the connector ships standalone and minds_admin does not depend on
# it).
_LEASE_HOLDING_POOL_STATUSES: Final[tuple[str, ...]] = (
    "leased",
    "stopping",
    "stopped",
    "starting",
    "crashed",
    "removing",
)
_LEASE_HOLDING_STATUSES_SQL: Final[str] = ", ".join(f"'{status}'" for status in _LEASE_HOLDING_POOL_STATUSES)


class RecordIdRepairConflictError(MindError):
    """A planned write matched nothing: the row changed between the plan and the apply."""


# The provider-kind prefix the connector's lease stub and the desktop's cloud
# records carry (``imbue_cloud_<account-slug>``).
_CLOUD_PROVIDER_KIND_PREFIX: Final[str] = "imbue_cloud"

_SELECT_LEASE_HOLDING_POOL_ROWS_SQL: Final[str] = (
    "SELECT id, host_id, agent_id, leased_to_user, status FROM pool_hosts "
    f"WHERE status IN ({_LEASE_HOLDING_STATUSES_SQL}) AND agent_id IS NOT NULL AND leased_to_user IS NOT NULL "
    "ORDER BY leased_at"
)

_SELECT_WORKSPACE_RECORDS_SQL: Final[str] = (
    "SELECT user_id, agent_id, host_id, state, revision, encrypted_secrets IS NOT NULL, backup_bucket, "
    "provider_kind FROM workspace_records ORDER BY created_at"
)

_REPOINT_POOL_HOST_AGENT_ID_SQL: Final[str] = "UPDATE pool_hosts SET agent_id = %s WHERE id = %s AND agent_id = %s"

_DELETE_ORPHAN_STUB_SQL: Final[str] = (
    "DELETE FROM workspace_records WHERE user_id = %s AND agent_id = %s AND state = 'active' "
    "AND revision = 1 AND encrypted_secrets IS NULL AND backup_bucket IS NULL"
)


class RepairPoolRow(FrozenModel):
    """The pool_hosts columns the repair reads for one lease-holding row."""

    host_db_id: str = Field(description="The pool_hosts row id")
    host_id: str = Field(description="The mngr host id")
    agent_id: str = Field(description="The services agent id the row currently carries")
    leased_to_user: str = Field(description="The owning user's 16-hex prefix")
    status: str = Field(description="The row's lifecycle status")


class RepairWorkspaceRecord(FrozenModel):
    """The workspace_records columns the repair reads for one record."""

    user_id: str = Field(description="The owner's full SuperTokens user id")
    agent_id: str = Field(description="The workspace id (the record's key with the owner)")
    host_id: str = Field(description="The machine the record names")
    state: str = Field(description="active or destroyed")
    revision: int = Field(description="The record's CAS revision (1 is the untouched lease stub)")
    is_holding_encrypted_secrets: bool = Field(description="Whether a client wrote a secrets blob")
    backup_bucket: str | None = Field(description="The backup bucket a client recorded, if any")
    provider_kind: str = Field(description="The mngr provider instance name the record carries")

    @property
    def user_id_prefix(self) -> str:
        return derive_user_id_prefix(self.user_id)

    @property
    def is_untouched_cloud_stub(self) -> bool:
        """Whether the record still has the shape the connector's lease-time stub is written with."""
        return (
            self.state == "active"
            and self.revision == 1
            and not self.is_holding_encrypted_secrets
            and self.backup_bucket is None
            and self.provider_kind.startswith(_CLOUD_PROVIDER_KIND_PREFIX)
        )


class AgentIdRepoint(FrozenModel):
    """One pool row whose agent id is moved to the owner's record for the same host."""

    host_db_id: str = Field(description="The pool_hosts row id")
    host_id: str = Field(description="The mngr host id both sides share")
    leased_to_user: str = Field(description="The owning user's 16-hex prefix")
    pool_agent_id: str = Field(description="The agent id the row carried (the baked one)")
    record_agent_id: str = Field(description="The agent id the owner's record carries (the rebuilt one)")
    record_state: str = Field(description="The matched record's state, so a tombstone repoint is visible")


class MismatchSkipReason(LowerCaseStrEnum):
    """Why a record-less lease was left alone."""

    # The owner has no record naming the row's host id at all (a lease from
    # before records existed, e.g. a retired cutover leftover).
    NO_RECORD = auto()
    # Several of the owner's records name the row's host id under different
    # agent ids; the operator resolves which one is the workspace.
    AMBIGUOUS = auto()
    # The only other records naming the row's host have the stub shape
    # (revision 1, no secrets, no backup bucket): a client's record for an
    # account without a master password and a workspace without a backup
    # bucket looks the same, so the operator tells them apart by hand.
    UNCONFIRMED_RECORD = auto()


class SkippedMismatch(FrozenModel):
    """A lease-holding row whose agent id has no client-written record and that the repair could not resolve."""

    host_db_id: str = Field(description="The pool_hosts row id")
    host_id: str = Field(description="The mngr host id")
    leased_to_user: str = Field(description="The owning user's 16-hex prefix")
    pool_agent_id: str = Field(description="The agent id the row carries")
    status: str = Field(description="The row's lifecycle status")
    reason: MismatchSkipReason = Field(description="Why it was skipped")
    candidate_agent_ids: tuple[str, ...] = Field(
        description="The owner's record ids naming the host (ambiguous and unconfirmed only)"
    )


class OrphanStubDeletion(FrozenModel):
    """A lease-time record stub that no lease matches by agent id once the repoints are applied."""

    user_id: str = Field(description="The owner's full SuperTokens user id")
    agent_id: str = Field(description="The stub's (baked) agent id")
    host_id: str = Field(description="The host the stub names, held by a lease under another agent id")


class RecordIdRepairPlan(FrozenModel):
    """What one repair run would change, computed from a snapshot of both tables."""

    repoints: tuple[AgentIdRepoint, ...] = Field(description="Pool rows to move to their record's agent id")
    skipped: tuple[SkippedMismatch, ...] = Field(description="Record-less rows left alone, with the reason")
    orphan_stub_deletions: tuple[OrphanStubDeletion, ...] = Field(description="Stubs to delete")

    @property
    def is_empty(self) -> bool:
        return not self.repoints and not self.orphan_stub_deletions


class RecordIdRepairRunReport(FrozenModel):
    """What one run of the repair command did: the plan it computed and whether it applied it."""

    is_applied: bool = Field(
        description="Whether the plan's writes were committed (False for a dry run or an empty plan)"
    )
    repointed_count: int = Field(description="Pool rows the plan moves to their record's agent id")
    deleted_stub_count: int = Field(description="Orphaned lease-time stubs the plan deletes")
    skipped_count: int = Field(description="Record-less rows left to the operator")
    plan: RecordIdRepairPlan = Field(description="The full plan, for the operator to read")


@pure
def _owner_records(records: Sequence[RepairWorkspaceRecord], user_id_prefix: str) -> list[RepairWorkspaceRecord]:
    return [record for record in records if record.user_id_prefix == user_id_prefix]


@pure
def _plan_repoints(
    pool_rows: Sequence[RepairPoolRow], records: Sequence[RepairWorkspaceRecord]
) -> tuple[list[AgentIdRepoint], list[SkippedMismatch]]:
    repoints: list[AgentIdRepoint] = []
    skipped: list[SkippedMismatch] = []
    for row in pool_rows:
        owner_records = _owner_records(records, row.leased_to_user)
        agent_id_matches = [record for record in owner_records if record.agent_id == row.agent_id]
        if any(not record.is_untouched_cloud_stub for record in agent_id_matches):
            continue
        other_records_naming_host = [
            record for record in owner_records if record.host_id == row.host_id and record.agent_id != row.agent_id
        ]
        # Only a client-written record can claim the host: a stub under some
        # other id says nothing about which workspace the row is.
        candidates = [record for record in other_records_naming_host if not record.is_untouched_cloud_stub]
        if len(candidates) == 1:
            (candidate,) = candidates
            repoints.append(
                AgentIdRepoint(
                    host_db_id=row.host_db_id,
                    host_id=row.host_id,
                    leased_to_user=row.leased_to_user,
                    pool_agent_id=row.agent_id,
                    record_agent_id=candidate.agent_id,
                    record_state=candidate.state,
                )
            )
        elif len(candidates) > 1:
            skipped.append(_skipped_mismatch(row, MismatchSkipReason.AMBIGUOUS, candidates))
        elif other_records_naming_host:
            skipped.append(_skipped_mismatch(row, MismatchSkipReason.UNCONFIRMED_RECORD, other_records_naming_host))
        elif not agent_id_matches:
            skipped.append(_skipped_mismatch(row, MismatchSkipReason.NO_RECORD, ()))
        else:
            # The lease's own stub is its only record so far (the desktop has
            # not pushed yet): consistent, nothing to repair.
            pass
    return repoints, skipped


@pure
def _skipped_mismatch(
    row: RepairPoolRow, reason: MismatchSkipReason, reported_records: Sequence[RepairWorkspaceRecord]
) -> SkippedMismatch:
    return SkippedMismatch(
        host_db_id=row.host_db_id,
        host_id=row.host_id,
        leased_to_user=row.leased_to_user,
        pool_agent_id=row.agent_id,
        status=row.status,
        reason=reason,
        candidate_agent_ids=tuple(sorted(record.agent_id for record in reported_records)),
    )


@pure
def _plan_orphan_stub_deletions(
    pool_rows: Sequence[RepairPoolRow],
    records: Sequence[RepairWorkspaceRecord],
    repoints: Sequence[AgentIdRepoint],
    skipped: Sequence[SkippedMismatch],
) -> list[OrphanStubDeletion]:
    # The stubs are judged against the pool as it will look once the repoints
    # land, so one run both repoints a row and drops the stub it strands. A
    # host whose row was skipped is left entirely to the operator: nothing
    # of it is deleted until they have resolved which record is the workspace.
    record_agent_id_by_host_db_id = {repoint.host_db_id: repoint.record_agent_id for repoint in repoints}
    effective_rows = [
        row.model_copy_update(
            to_update(row.field_ref().agent_id, record_agent_id_by_host_db_id.get(row.host_db_id, row.agent_id))
        )
        for row in pool_rows
    ]
    skipped_host_keys = {(mismatch.leased_to_user, mismatch.host_id) for mismatch in skipped}
    deletions: list[OrphanStubDeletion] = []
    for record in records:
        if not record.is_untouched_cloud_stub:
            continue
        if (record.user_id_prefix, record.host_id) in skipped_host_keys:
            continue
        owner_rows = [row for row in effective_rows if row.leased_to_user == record.user_id_prefix]
        if any(row.agent_id == record.agent_id for row in owner_rows):
            continue
        if not any(row.host_id == record.host_id and row.agent_id != record.agent_id for row in owner_rows):
            continue
        deletions.append(OrphanStubDeletion(user_id=record.user_id, agent_id=record.agent_id, host_id=record.host_id))
    return deletions


@pure
def plan_record_id_repair(
    pool_rows: Sequence[RepairPoolRow], records: Sequence[RepairWorkspaceRecord]
) -> RecordIdRepairPlan:
    """Decide the repoints, the skipped mismatches, and the orphan stubs to delete."""
    repoints, skipped = _plan_repoints(pool_rows, records)
    deletions = _plan_orphan_stub_deletions(pool_rows, records, repoints, skipped)
    return RecordIdRepairPlan(repoints=tuple(repoints), skipped=tuple(skipped), orphan_stub_deletions=tuple(deletions))


def fetch_lease_holding_pool_rows(conn: Any) -> list[RepairPoolRow]:
    """Every lease-holding pool row that carries an agent id and an owner, oldest lease first."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_LEASE_HOLDING_POOL_ROWS_SQL)
        rows = cur.fetchall()
    return [
        RepairPoolRow(
            host_db_id=str(row[0]),
            host_id=str(row[1]),
            agent_id=str(row[2]),
            leased_to_user=str(row[3]),
            status=str(row[4]),
        )
        for row in rows
    ]


def fetch_workspace_records(conn: Any) -> list[RepairWorkspaceRecord]:
    """Every workspace record, in the columns the repair judges by."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_WORKSPACE_RECORDS_SQL)
        rows = cur.fetchall()
    return [
        RepairWorkspaceRecord(
            user_id=str(row[0]),
            agent_id=str(row[1]),
            host_id=str(row[2]),
            state=str(row[3]),
            revision=int(row[4]),
            is_holding_encrypted_secrets=bool(row[5]),
            backup_bucket=row[6],
            provider_kind=str(row[7] or ""),
        )
        for row in rows
    ]


def apply_record_id_repair(conn: Any, plan: RecordIdRepairPlan) -> None:
    """Apply the plan's repoints and deletions in one transaction.

    Each statement is a compare-and-swap on the state the plan saw; the first
    one that matches nothing raises ``RecordIdRepairConflictError``, which
    rolls the whole transaction back (the connection is the transaction
    context manager), so a re-run re-plans from the current tables instead of
    writing over a concurrent change.
    """
    with conn:
        with conn.cursor() as cur:
            for repoint in plan.repoints:
                cur.execute(
                    _REPOINT_POOL_HOST_AGENT_ID_SQL,
                    (repoint.record_agent_id, repoint.host_db_id, repoint.pool_agent_id),
                )
                if cur.rowcount != 1:
                    raise RecordIdRepairConflictError(
                        f"pool row {repoint.host_db_id} no longer carries agent id {repoint.pool_agent_id}"
                    )
            for deletion in plan.orphan_stub_deletions:
                cur.execute(_DELETE_ORPHAN_STUB_SQL, (deletion.user_id, deletion.agent_id))
                if cur.rowcount != 1:
                    raise RecordIdRepairConflictError(
                        f"record {deletion.agent_id} of user {deletion.user_id[:8]} is no longer an untouched stub"
                    )


def run_record_id_repair(conn: Any, *, is_execute: bool) -> RecordIdRepairRunReport:
    """Plan from the current tables and, only with ``is_execute`` and something to change, apply the plan."""
    plan = plan_record_id_repair(fetch_lease_holding_pool_rows(conn), fetch_workspace_records(conn))
    is_applied = is_execute and not plan.is_empty
    if is_applied:
        apply_record_id_repair(conn, plan)
    return RecordIdRepairRunReport(
        is_applied=is_applied,
        repointed_count=len(plan.repoints),
        deleted_stub_count=len(plan.orphan_stub_deletions),
        skipped_count=len(plan.skipped),
        plan=plan,
    )
