"""``minds_services`` test: the analytics collection loop against a real env's databases.

Exercises the runner's real SQL against the shared env's live Postgres: the
explorer consent sync (prefix join on ``account_entitlements``), online
workspace enumeration from ``pool_hosts``, the poll's advisory lock, and the
audited refused-hop path -- the ops tables are created from the committed
analytics migrations in a per-test schema of the same database, and a
fixture explorer account + leased workspace row (pointing at an unreachable
address) drive one full ``run_collection_poll_with_connections`` pass whose
SSH hop is genuinely refused and lands in the real ``collection_runs`` audit.

The in-workspace half (script injection, redaction, protocol) is covered by
the analytics acceptance test (``test_injected_script.py``); this test owns
the server-side loop against real infrastructure. Collecting from a real
leased workspace additionally requires the tier's pool SSH key and a live
lease, which stays a manual bringup-verification step
(apps/analytics/docs/bringup.md).
"""

import re
from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from pydantic import SecretStr

from imbue.analytics.collection import OUTCOME_SSH_REFUSED
from imbue.analytics.collection import collect_over_ssh
from imbue.analytics.collection import run_collection_poll_with_connections
from imbue.analytics.settings import CollectionSettings
from imbue.analytics.testing import build_fixture_analytics_session
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.modal_app_kit.database import direct_database_url

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "apps" / "analytics" / "migrations"

# An Ed25519 key generated for this test alone (never authorized anywhere);
# the SSH hop must be REFUSED, that is the point.
_THROWAWAY_ED25519_PEM = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
QyNTUxOQAAACBXshYFY+TQwMH3cjhARz2NEHQqioa3Wcc0Qezg/FpVNwAAAKBb7G0SW+xt
EgAAAAtzc2gtZWQyNTUxOQAAACBXshYFY+TQwMH3cjhARz2NEHQqioa3Wcc0Qezg/FpVNw
AAAEBCV3lnJaHsTPu7FUsMCUIS/zTertXulrnR7IZiGnlYRVeyFgVj5NDAwfdyOEBHPY0Q
dCqKhrdZxzRB7OD8WlU3AAAAGWFuYWx5dGljcy1jb2xsZWN0aW9uLXRlc3QBAgME
-----END OPENSSH PRIVATE KEY-----
"""


@pytest.fixture
def analytics_ops_schema(shared_env: Callable[[str], SharedEnvHandle]) -> Generator[tuple[str, str], None, None]:
    """A per-test schema in the shared env's DB holding the real analytics ops tables.

    Yields (dsn with search_path pinned to the schema, schema name); drops the
    schema in teardown.
    """
    handle = shared_env("default")
    # The published DSN points at Neon's PgBouncer pooler, where session
    # state (search_path, advisory locks) is unsafe; the whole test uses the
    # direct endpoint.
    dsn = direct_database_url(handle.neon_host_pool_dsn.get_secret_value())
    schema_name = f"analytics_test_{uuid4().hex}"
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {schema_name}")
            # Transaction-local, so nothing leaks past the fixture's commit.
            cursor.execute(f"SET LOCAL search_path TO {schema_name}")
            for migration_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                cursor.execute(migration_file.read_text())
        connection.commit()
    finally:
        connection.close()
    separator = "&" if "?" in dsn else "?"
    scoped_dsn = f"{dsn}{separator}options=-csearch_path%3D{schema_name}"
    try:
        yield scoped_dsn, schema_name
    finally:
        cleanup = psycopg2.connect(dsn)
        try:
            with cleanup.cursor() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE")
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture
def explorer_workspace_rows(
    shared_env: Callable[[str], SharedEnvHandle],
) -> Generator[tuple[str, str, str], None, None]:
    """A fixture explorer account + leased (unreachable) workspace row in the real connector DB.

    Yields (connector DSN, account user id, host id); deletes both rows in
    teardown.
    """
    handle = shared_env("default")
    dsn = direct_database_url(handle.neon_host_pool_dsn.get_secret_value())
    user_id = str(uuid4())
    user_id_prefix = user_id.replace("-", "")[:16]
    host_id = f"host-{uuid4().hex}"
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO account_entitlements (user_id, user_id_prefix, plan_name, max_remote_workspaces,"
                " max_buckets, max_total_bucket_bytes, monthly_llm_spend_usd, max_active_synced_workspaces,"
                " max_total_workspaces)"
                " VALUES (%s, %s, 'explorer', 1, 1, 1073741824, 0, 10, 10)",
                (user_id, user_id_prefix),
            )
            cursor.execute(
                "INSERT INTO pool_hosts (id, vps_address, vps_instance_id, agent_id, host_id, host_name,"
                " ssh_port, ssh_user, container_ssh_port, status, leased_to_user, created_at)"
                # 127.0.0.1:9 (discard) refuses SSH immediately; this row must
                # never gain a reachable placement.
                " VALUES (gen_random_uuid(), '127.0.0.1', %s, %s, %s, %s, 9, 'user', 9, 'leased', %s, NOW())",
                (
                    f"analytics-test-{uuid4().hex}",
                    f"agent-{uuid4().hex}",
                    host_id,
                    f"analytics-collection-test-{uuid4().hex}",
                    user_id_prefix,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    try:
        yield dsn, user_id, host_id
    finally:
        cleanup = psycopg2.connect(dsn)
        try:
            with cleanup.cursor() as cursor:
                cursor.execute("DELETE FROM pool_hosts WHERE host_id = %s", (host_id,))
                cursor.execute("DELETE FROM account_entitlements WHERE user_id = %s", (user_id,))
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.mark.timeout(180)
def test_collection_poll_consents_enumerates_and_audits_a_refused_hop_against_real_databases(
    analytics_ops_schema: tuple[str, str],
    explorer_workspace_rows: tuple[str, str, str],
) -> None:
    ops_dsn, _schema_name = analytics_ops_schema
    rsc_dsn, user_id, host_id = explorer_workspace_rows
    collection_settings = CollectionSettings(
        pool_ssh_private_key=SecretStr(_THROWAWAY_ED25519_PEM),
        interval_seconds=3600,
        parallelism=2,
        workspace_timeout_seconds=30,
        run_budget_bytes=1024 * 1024,
    )
    lake_connection = build_fixture_analytics_session()
    ops_connection = psycopg2.connect(ops_dsn)
    rsc_connection = psycopg2.connect(rsc_dsn)
    try:
        counters = run_collection_poll_with_connections(
            collection_settings=collection_settings,
            lake_connection=lake_connection,
            ops_connection=ops_connection,
            rsc_connection=rsc_connection,
            collect_fn=collect_over_ssh,
        )

        # The fixture workspace was due and its (real) SSH hop was refused.
        assert counters["workspaces_due"] >= 1
        assert counters["workspaces_collected"] == 0

        with ops_connection.cursor() as cursor:
            cursor.execute("SELECT is_consenting FROM consent_ledger WHERE account_id = %s", (user_id,))
            consent_row = cursor.fetchone()
            assert consent_row is not None and consent_row[0] is True

            cursor.execute("SELECT outcome, account_id, detail FROM collection_runs WHERE host_id = %s", (host_id,))
            audit_rows = cursor.fetchall()
        assert len(audit_rows) == 1
        outcome, audited_account_id, detail = audit_rows[0]
        assert outcome == OUTCOME_SSH_REFUSED
        assert audited_account_id == user_id
        assert re.search(r"refused", detail)

        # Release the advisory lock, then verify a second poll inside the
        # interval does not touch the workspace again.
        ops_connection.close()
        second_ops_connection = psycopg2.connect(ops_dsn)
        try:
            second_counters = run_collection_poll_with_connections(
                collection_settings=collection_settings,
                lake_connection=lake_connection,
                ops_connection=second_ops_connection,
                rsc_connection=rsc_connection,
                collect_fn=collect_over_ssh,
            )
        finally:
            second_ops_connection.close()
        assert second_counters["workspaces_due"] == 0
    finally:
        rsc_connection.close()
        ops_connection.close()
        lake_connection.close()
