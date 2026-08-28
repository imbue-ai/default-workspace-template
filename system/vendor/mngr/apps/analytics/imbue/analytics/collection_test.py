import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import duckdb
import pytest
from inline_snapshot import snapshot
from pydantic import SecretStr

from imbue.analytics.collection import OUTCOME_LAKE_ERROR
from imbue.analytics.collection import OUTCOME_OK
from imbue.analytics.collection import OUTCOME_SSH_REFUSED
from imbue.analytics.collection import SshCollectionResult
from imbue.analytics.collection import _build_run_command
from imbue.analytics.collection import _due_workspaces
from imbue.analytics.collection import compute_script_version
from imbue.analytics.collection import load_injected_script_files
from imbue.analytics.collection import process_collection_result
from imbue.analytics.collection import run_collection_poll_with_connections
from imbue.analytics.consent import CollectableWorkspace
from imbue.analytics.errors import CollectionError
from imbue.analytics.mock_ops_db_test import FailingOpsConnection
from imbue.analytics.mock_ops_db_test import RoutingFakeConnection
from imbue.analytics.protocol import parse_collection_output
from imbue.analytics.settings import CollectionSettings
from imbue.analytics.testing import build_fixture_analytics_session

_NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _workspace(host_id: str = "host-abc") -> CollectableWorkspace:
    return CollectableWorkspace(
        host_db_id="11111111-2222-3333-4444-555555555555",
        host_id=host_id,
        account_id="aaaa0000-1111-2222-3333-444455556666",
        vps_address="203.0.113.5",
        ssh_port=2201,
        container_ssh_port=2202,
        ssh_user="user",
        container_host_public_key="ssh-ed25519 BAKEKEY",
        outer_host_public_key=None,
    )


def _collection_settings() -> CollectionSettings:
    return CollectionSettings(
        pool_ssh_private_key=SecretStr("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n"),
        interval_seconds=3600,
        parallelism=2,
        workspace_timeout_seconds=600,
        run_budget_bytes=1024 * 1024,
    )


def _ok_ssh_result() -> SshCollectionResult:
    lines = [
        json.dumps(
            {
                "source": "client_activity",
                "record": {
                    "timestamp": "2026-08-18T11:59:00Z",
                    "event_id": "evt-c1",
                    "type": "message",
                    "source": "client_activity",
                },
            }
        ),
        json.dumps(
            {
                "source": "transcripts",
                "record": {
                    "timestamp": "2026-08-18T11:58:00Z",
                    "event_id": "evt-t1",
                    "type": "user_message",
                    "source": "claude",
                    "content": "[already redacted]",
                },
            }
        ),
        json.dumps(
            {
                "source": "run_summary",
                "record_count_by_source": {"client_activity": 1, "transcripts": 1},
                "cursor_by_source": {"client_activity": '{"a": 1}', "transcripts": '{"b": 2}'},
                "read_bytes": 100,
                "is_budget_exhausted": False,
                "script_version": "hash",
            }
        ),
    ]
    return SshCollectionResult(
        outcome=OUTCOME_OK,
        parsed=parse_collection_output("\n".join(lines)),
        stdout_bytes=512,
        presented_container_key="ssh-ed25519 BAKEKEY",
        presented_vm_key=None,
        latchkey_record={
            "timestamp": _NOW.isoformat(),
            "event_id": "latchkey-state-run-1",
            "type": "latchkey_state",
            "source": "latchkey_state",
            "is_gateway_dir_present": True,
            "permissions_byte_count": 120,
            "is_credentials_store_present": True,
        },
        detail="",
    )


def _ops_fake(extra_rows: dict[str, list[tuple[Any, ...]]] | None = None) -> RoutingFakeConnection:
    rows: dict[str, list[tuple[Any, ...]]] = {
        "pg_try_advisory_lock": [(True,)],
        "FROM consent_ledger": [],
        "FROM collection_host_keys": [],
        "FROM collection_cursors": [],
        "FROM collection_runs": [],
    }
    if extra_rows:
        rows.update(extra_rows)
    return RoutingFakeConnection(rows)


def test_process_collection_result_writes_lakes_cursors_and_audit() -> None:
    lake = build_fixture_analytics_session()
    ops = _ops_fake()

    outcome = process_collection_result(
        lake_connection=lake,
        ops_connection=ops,
        workspace=_workspace(),
        ssh_result=_ok_ssh_result(),
        run_id="run-1",
        script_version="hash",
        started_at=_NOW,
    )

    assert outcome.outcome == OUTCOME_OK
    # The latchkey record lands beside the script's own metrics rows.
    assert outcome.metrics_rows == 2
    assert outcome.transcript_rows == 1
    metrics_rows = lake.execute(
        "SELECT event_id, feed_source, host_id, account_id, payload FROM metrics.raw.workspace_events ORDER BY event_id"
    ).fetchall()
    assert [(row[0], row[1]) for row in metrics_rows] == [
        ("evt-c1", "client_activity"),
        ("latchkey-state-run-1", "latchkey_state"),
    ]
    assert metrics_rows[0][2] == "host-abc"
    assert metrics_rows[0][3] == "aaaa0000-1111-2222-3333-444455556666"
    transcript_rows = lake.execute("SELECT event_id, payload FROM transcripts.raw.transcript_events").fetchall()
    assert transcript_rows[0][0] == "evt-t1"
    assert json.loads(transcript_rows[0][1])["content"] == "[already redacted]"

    executed = ops.routing_cursor.executed
    cursor_writes = [parameters for statement, parameters in executed if "INSERT INTO collection_cursors" in statement]
    assert {parameters[1] for parameters in cursor_writes} == {"client_activity", "transcripts"}
    audit_rows = [parameters for statement, parameters in executed if "INSERT INTO collection_runs" in statement]
    assert len(audit_rows) == 1
    assert audit_rows[0][5] == OUTCOME_OK
    key_writes = [parameters for statement, parameters in executed if "INSERT INTO collection_host_keys" in statement]
    assert len(key_writes) == 1
    assert key_writes[0][1] == "container"


def test_process_collection_result_flags_a_changed_host_key() -> None:
    lake = build_fixture_analytics_session()
    ops = _ops_fake({"FROM collection_host_keys": [("container", "ssh-ed25519 OLDKEY")]})

    outcome = process_collection_result(
        lake_connection=lake,
        ops_connection=ops,
        workspace=_workspace(),
        ssh_result=_ok_ssh_result(),
        run_id="run-1",
        script_version="hash",
        started_at=_NOW,
    )

    assert outcome.is_host_key_changed is True
    audit_rows = [
        parameters
        for statement, parameters in ops.routing_cursor.executed
        if "INSERT INTO collection_runs" in statement
    ]
    assert audit_rows[0][11] is True


def test_process_collection_result_skips_cursors_when_the_lake_write_fails() -> None:
    # A session without the raw tables makes every insert fail.
    broken_lake = duckdb.connect()
    ops = _ops_fake()

    outcome = process_collection_result(
        lake_connection=broken_lake,
        ops_connection=ops,
        workspace=_workspace(),
        ssh_result=_ok_ssh_result(),
        run_id="run-1",
        script_version="hash",
        started_at=_NOW,
    )

    assert outcome.outcome == OUTCOME_LAKE_ERROR
    cursor_writes = [
        parameters
        for statement, parameters in ops.routing_cursor.executed
        if "INSERT INTO collection_cursors" in statement
    ]
    assert cursor_writes == []
    audit_rows = [
        parameters
        for statement, parameters in ops.routing_cursor.executed
        if "INSERT INTO collection_runs" in statement
    ]
    assert audit_rows[0][5] == OUTCOME_LAKE_ERROR


def test_run_collection_poll_collects_due_workspaces_and_skips_recent_ones() -> None:
    lake = build_fixture_analytics_session()
    ops = _ops_fake(
        {
            "FROM collection_runs": [("host-recent", datetime.now(timezone.utc) - timedelta(seconds=10))],
        }
    )
    rsc = RoutingFakeConnection(
        {
            "account_entitlements": [("aaaa000011112222", "aaaa0000-1111-2222-3333-444455556666")],
            "FROM pool_hosts": [
                (
                    "11111111-2222-3333-4444-555555555555",
                    "host-due",
                    "aaaa000011112222",
                    "203.0.113.5",
                    None,
                    2202,
                    "user",
                    None,
                    None,
                ),
                (
                    "11111111-2222-3333-4444-666666666666",
                    "host-recent",
                    "aaaa000011112222",
                    "203.0.113.6",
                    None,
                    2203,
                    "user",
                    None,
                    None,
                ),
            ],
        }
    )
    collected_host_ids: list[str] = []

    def fake_collect(workspace: CollectableWorkspace, *args: Any) -> SshCollectionResult:
        collected_host_ids.append(workspace.host_id)
        return _ok_ssh_result()

    counters = run_collection_poll_with_connections(
        collection_settings=_collection_settings(),
        lake_connection=lake,
        ops_connection=ops,
        rsc_connection=rsc,
        collect_fn=fake_collect,
    )

    assert collected_host_ids == ["host-due"]
    assert counters["workspaces_due"] == 1
    assert counters["workspaces_collected"] == 1
    assert counters["workspaces_failed"] == 0
    assert counters["metrics_rows"] == 2
    assert counters["transcript_rows"] == 1


def test_run_collection_poll_records_failed_workspaces_in_the_audit_only() -> None:
    lake = build_fixture_analytics_session()
    ops = _ops_fake()
    rsc = RoutingFakeConnection(
        {
            "account_entitlements": [("aaaa000011112222", "aaaa0000-1111-2222-3333-444455556666")],
            "FROM pool_hosts": [
                (
                    "11111111-2222-3333-4444-555555555555",
                    "host-refused",
                    "aaaa000011112222",
                    "203.0.113.5",
                    None,
                    2202,
                    "user",
                    None,
                    None,
                )
            ],
        }
    )

    def refusing_collect(*args: Any) -> SshCollectionResult:
        return SshCollectionResult(
            outcome=OUTCOME_SSH_REFUSED,
            parsed=None,
            stdout_bytes=0,
            presented_container_key=None,
            presented_vm_key=None,
            latchkey_record=None,
            detail="container hop refused: authentication failed",
        )

    counters = run_collection_poll_with_connections(
        collection_settings=_collection_settings(),
        lake_connection=lake,
        ops_connection=ops,
        rsc_connection=rsc,
        collect_fn=refusing_collect,
    )

    assert counters["workspaces_failed"] == 1
    assert counters["workspaces_collected"] == 0
    audit_rows = [
        parameters
        for statement, parameters in ops.routing_cursor.executed
        if "INSERT INTO collection_runs" in statement
    ]
    assert len(audit_rows) == 1
    assert audit_rows[0][5] == OUTCOME_SSH_REFUSED
    assert "refused" in audit_rows[0][12]


def test_run_collection_poll_survives_an_unparsable_stored_cursor() -> None:
    """A poisoned/legacy cursor row is skipped with a warning, never sinking the poll."""
    lake = build_fixture_analytics_session()
    ops = _ops_fake(
        {
            "FROM collection_cursors": [("transcripts", "{not json"), ("client_activity", '{"offset": 10}')],
        }
    )
    rsc = RoutingFakeConnection(
        {
            "account_entitlements": [("aaaa000011112222", "aaaa0000-1111-2222-3333-444455556666")],
            "FROM pool_hosts": [
                (
                    "11111111-2222-3333-4444-555555555555",
                    "host-due",
                    "aaaa000011112222",
                    "203.0.113.5",
                    None,
                    2202,
                    "user",
                    None,
                    None,
                )
            ],
        }
    )
    received_cursors_json: list[str] = []

    def capturing_collect(workspace: CollectableWorkspace, *args: Any) -> SshCollectionResult:
        received_cursors_json.append(args[3])
        return _ok_ssh_result()

    counters = run_collection_poll_with_connections(
        collection_settings=_collection_settings(),
        lake_connection=lake,
        ops_connection=ops,
        rsc_connection=rsc,
        collect_fn=capturing_collect,
    )

    assert counters["workspaces_collected"] == 1
    # The bad cursor is dropped; the valid one still reaches the script.
    assert json.loads(received_cursors_json[0]) == {"client_activity": {"offset": 10}}


def test_run_collection_poll_wraps_ops_db_failures_in_collection_error() -> None:
    """A dead ops DB must surface as CollectionError so the job records a failure row."""
    with pytest.raises(CollectionError, match="Ops-database access failed"):
        run_collection_poll_with_connections(
            collection_settings=_collection_settings(),
            lake_connection=duckdb.connect(),
            ops_connection=FailingOpsConnection(),
            rsc_connection=RoutingFakeConnection({}),
            collect_fn=lambda *args: _ok_ssh_result(),
        )


def test_run_collection_poll_skips_cleanly_when_another_run_holds_the_lock() -> None:
    ops = _ops_fake({"pg_try_advisory_lock": [(False,)]})

    counters = run_collection_poll_with_connections(
        collection_settings=_collection_settings(),
        lake_connection=duckdb.connect(),
        ops_connection=ops,
        rsc_connection=RoutingFakeConnection({}),
        collect_fn=lambda *args: _ok_ssh_result(),
    )

    assert counters == {"skipped_overlapping": 1}


def test_script_version_is_a_deterministic_content_hash_over_the_injected_files() -> None:
    files = load_injected_script_files()

    assert set(files) == snapshot(
        {
            "collect.py",
            "imbue/__init__.py",
            "imbue/analytics/__init__.py",
            "imbue/analytics/injected/__init__.py",
            "imbue/analytics/injected/workspace_feeds.py",
            "imbue/analytics/injected/workspace_redaction.py",
        }
    )
    assert "PEP 723" not in files["imbue/__init__.py"]
    assert compute_script_version(files) == compute_script_version(load_injected_script_files())
    assert compute_script_version(files) != compute_script_version({**files, "collect.py": "changed"})


def test_build_run_command_bounds_the_run_and_names_every_argument() -> None:
    command = _build_run_command("run-1", "hash-1", 1024, 600)

    assert command == snapshot(
        'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH" && cd /home/user/workspace && timeout 600 uv run'
        " --script /home/user/workspace/data/.imbue/analytics/collect.py --run-id run-1 --script-version hash-1"
        " --workspace-root /home/user/workspace --host-dir /home/user/.mngr --cursors-file"
        " /home/user/workspace/data/.imbue/analytics/cursors.json --budget-bytes 1024"
    )


def test_due_workspaces_filters_on_the_last_attempt() -> None:
    now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    workspaces = [_workspace("host-old"), _workspace("host-fresh"), _workspace("host-never")]
    last_attempts = {
        "host-old": now - timedelta(hours=2),
        "host-fresh": now - timedelta(minutes=5),
    }

    due = _due_workspaces(workspaces, last_attempts, interval_seconds=3600, now=now)

    assert [workspace.host_id for workspace in due] == ["host-old", "host-never"]
