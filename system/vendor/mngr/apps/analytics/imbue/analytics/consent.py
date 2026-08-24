"""Consent and enumeration for in-workspace collection.

Consent IS explorer-plan membership: every poll reads the current explorer
set from the connector's product database (read-only role) and diffs it into
the ops ``consent_ledger``. Leaving the plan stops collection at the next poll
and deletes nothing. Enumeration then lists the online, non-transitioning
pool workspaces leased to consenting accounts.

The pool_hosts lease column carries the 16-hex user-id prefix, so the join to
``account_entitlements`` goes through ``user_id_prefix``; the full SuperTokens
user id is the analytics key everywhere downstream.
"""

import logging
from datetime import datetime
from typing import Any

import psycopg2
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

import imbue.analytics.ops_db as ops_db
from imbue.analytics.errors import CollectionError

logger = logging.getLogger(__name__)


class ConsentSyncResult(BaseModel):
    """What one consent-ledger sync changed."""

    model_config = ConfigDict(frozen=True)

    consenting_account_count: int = Field(description="Accounts currently on the explorer plan")
    newly_consenting_count: int = Field(description="Accounts that entered the plan since the last sync")
    newly_revoked_count: int = Field(description="Accounts that left the plan since the last sync")


class CollectableWorkspace(BaseModel):
    """One online explorer workspace the poll may collect from."""

    model_config = ConfigDict(frozen=True)

    host_db_id: str = Field(description="pool_hosts row id")
    host_id: str = Field(description="mngr host id (host-<32hex>)")
    account_id: str = Field(description="Full SuperTokens user id of the leaseholder")
    vps_address: str = Field(description="Box public address")
    ssh_port: int | None = Field(description="VM-root forwarded port (the optional VM hop)")
    container_ssh_port: int = Field(description="Container forwarded port (the primary hop)")
    ssh_user: str = Field(description="SSH user for both endpoints (as the connector uses it)")
    container_host_public_key: str | None = Field(description="Bake-time container sshd key, when recorded")
    outer_host_public_key: str | None = Field(description="Bake-time VM-root sshd key, when recorded")


def read_explorer_accounts(rsc_connection: Any) -> dict[str, str]:
    """Current explorer-plan accounts as {user_id_prefix: full user id}.

    Raises CollectionError when the connector database cannot be read (the
    whole poll fails rather than treating an outage as an empty plan).
    """
    try:
        with rsc_connection.cursor() as cursor:
            cursor.execute("SELECT user_id_prefix, user_id FROM account_entitlements WHERE plan_name = 'explorer'")
            rows = cursor.fetchall()
    except psycopg2.Error as e:
        raise CollectionError("Cannot read explorer accounts from the connector database") from e
    return {str(row[0]): str(row[1]) for row in rows}


def sync_consent_ledger(ops_connection: Any, explorer_account_ids: set[str], now: datetime) -> ConsentSyncResult:
    """Diff the explorer set into the ops consent_ledger.

    Flips is_consenting on for accounts that entered the plan and off for
    accounts that left it; leaving deletes nothing. Raises CollectionError
    when the ledger cannot be read or written.
    """
    try:
        ledger = ops_db.read_consent_ledger(ops_connection)
        newly_consenting = sorted(
            account_id for account_id in explorer_account_ids if not ledger.get(account_id, False)
        )
        newly_revoked = sorted(
            account_id
            for account_id, is_consenting in ledger.items()
            if is_consenting and account_id not in explorer_account_ids
        )
        for account_id in newly_consenting:
            ops_db.set_consent(ops_connection, account_id=account_id, is_consenting=True, now=now)
        for account_id in newly_revoked:
            ops_db.set_consent(ops_connection, account_id=account_id, is_consenting=False, now=now)
    except psycopg2.Error as e:
        raise CollectionError("Cannot sync the consent ledger") from e
    if newly_consenting or newly_revoked:
        logger.info(
            "Consent ledger synced: %d consenting (%d new, %d revoked)",
            len(explorer_account_ids),
            len(newly_consenting),
            len(newly_revoked),
        )
    return ConsentSyncResult(
        consenting_account_count=len(explorer_account_ids),
        newly_consenting_count=len(newly_consenting),
        newly_revoked_count=len(newly_revoked),
    )


def list_online_explorer_workspaces(
    rsc_connection: Any, account_id_by_prefix: dict[str, str]
) -> list[CollectableWorkspace]:
    """Online (leased, fully-placed) workspaces of consenting accounts.

    A row mid stop/start transition has NULL placement columns or a
    non-``leased`` status and is skipped until it settles.

    Raises CollectionError when the connector database cannot be read.
    """
    if not account_id_by_prefix:
        return []
    try:
        with rsc_connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, host_id, leased_to_user, vps_address, ssh_port, container_ssh_port, ssh_user,"
                " container_host_public_key, outer_host_public_key"
                " FROM pool_hosts"
                " WHERE status = 'leased' AND leased_to_user = ANY(%s)"
                " AND vps_address IS NOT NULL AND container_ssh_port IS NOT NULL",
                (sorted(account_id_by_prefix.keys()),),
            )
            rows = cursor.fetchall()
    except psycopg2.Error as e:
        raise CollectionError("Cannot enumerate online explorer workspaces") from e
    workspaces: list[CollectableWorkspace] = []
    for row in rows:
        workspaces.append(
            CollectableWorkspace(
                host_db_id=str(row[0]),
                host_id=str(row[1]),
                account_id=account_id_by_prefix[str(row[2])],
                vps_address=str(row[3]),
                ssh_port=int(row[4]) if row[4] is not None else None,
                container_ssh_port=int(row[5]),
                ssh_user=str(row[6] or "root"),
                container_host_public_key=row[7],
                outer_host_public_key=row[8],
            )
        )
    return workspaces
