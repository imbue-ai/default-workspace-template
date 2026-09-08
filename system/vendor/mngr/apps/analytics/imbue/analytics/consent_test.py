from datetime import datetime
from datetime import timezone

from imbue.analytics.consent import list_online_explorer_workspaces
from imbue.analytics.consent import read_explorer_accounts
from imbue.analytics.consent import sync_consent_ledger
from imbue.analytics.mock_ops_db_test import RoutingFakeConnection

_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def test_read_explorer_accounts_maps_prefixes_to_full_ids() -> None:
    rsc = RoutingFakeConnection(
        {"account_entitlements": [("aaaa000011112222", "aaaa0000-1111-2222-3333-444455556666")]}
    )

    accounts = read_explorer_accounts(rsc)

    assert accounts == {"aaaa000011112222": "aaaa0000-1111-2222-3333-444455556666"}
    statement, _parameters = rsc.routing_cursor.executed[0]
    assert "plan_name = 'explorer'" in statement


def test_sync_consent_ledger_flips_membership_both_ways_and_counts() -> None:
    ops = RoutingFakeConnection(
        {"FROM consent_ledger": [("user-stays", True), ("user-leaves", True), ("user-already-off", False)]}
    )

    result = sync_consent_ledger(ops, {"user-stays", "user-joins"}, _NOW)

    assert result.consenting_account_count == 2
    assert result.newly_consenting_count == 1
    assert result.newly_revoked_count == 1
    upserts = [
        parameters
        for statement, parameters in ops.routing_cursor.executed
        if "INSERT INTO consent_ledger" in statement
    ]
    assert (("user-joins", True, _NOW, _NOW)) in upserts
    assert (("user-leaves", False, _NOW, _NOW)) in upserts
    # An account that already left stays off without a redundant write.
    assert not any(parameters[0] == "user-already-off" for parameters in upserts)


def test_list_online_explorer_workspaces_maps_rows_and_skips_when_no_accounts() -> None:
    rsc = RoutingFakeConnection(
        {
            "FROM pool_hosts": [
                (
                    "11111111-2222-3333-4444-555555555555",
                    "host-abc",
                    "aaaa000011112222",
                    "203.0.113.5",
                    2201,
                    2202,
                    "user",
                    "ssh-ed25519 CONTAINERKEY",
                    None,
                )
            ]
        }
    )
    account_id_by_prefix = {"aaaa000011112222": "aaaa0000-1111-2222-3333-444455556666"}

    workspaces = list_online_explorer_workspaces(rsc, account_id_by_prefix)

    assert len(workspaces) == 1
    workspace = workspaces[0]
    assert workspace.host_id == "host-abc"
    assert workspace.account_id == "aaaa0000-1111-2222-3333-444455556666"
    assert workspace.container_ssh_port == 2202
    assert workspace.ssh_port == 2201
    assert workspace.outer_host_public_key is None
    statement, _parameters = rsc.routing_cursor.executed[0]
    assert "status = 'leased'" in statement
    assert "container_ssh_port IS NOT NULL" in statement

    assert list_online_explorer_workspaces(rsc, {}) == []
