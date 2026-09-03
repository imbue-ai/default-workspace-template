"""Unit tests for the local ``bulk_delete_accounts.py`` operator tool.

The pure helpers, the ``_PhaseRunner`` progress/resume logic (against a fake
core client), and the ``SupertokensCoreClient`` response handling (against an
in-process ``httpx.MockTransport``) are covered here; the live SuperTokens /
Neon I/O is exercised operationally via the tool's dry-run default (absent
``--execute``), not in unit tests.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts import bulk_delete_accounts


class _RecordingCursor:
    """Fake psycopg2 cursor that records executed statements and reports a fixed rowcount."""

    def __init__(self, rowcount: int) -> None:
        self._fixed_rowcount = rowcount
        self.executed: list[str] = []
        self.executed_params: list[tuple[Any, ...]] = []
        self.copied: list[tuple[str, str]] = []
        self.rowcount = 0

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append(sql)
        self.executed_params.append(params)
        self.rowcount = self._fixed_rowcount

    def copy_expert(self, sql: str, buffer: Any) -> None:
        self.copied.append((sql, buffer.read()))

    def fetchone(self) -> tuple[int]:
        return (self._fixed_rowcount,)


class _RecordingConnection:
    """Fake psycopg2 connection handing out a single recording cursor."""

    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RecordingCursor:
        return self._cursor


class _FakeCoreClient(bulk_delete_accounts.SupertokensCoreClientInterface):
    """Fake core client that records calls and fails for configured user ids."""

    def __init__(self, failing_user_ids: set[str], revoked_sessions_per_user: int) -> None:
        self._failing_user_ids = failing_user_ids
        self._revoked_sessions_per_user = revoked_sessions_per_user
        self.removed_user_ids: list[str] = []
        self.revoked_user_ids: list[str] = []

    def _fail_if_configured(self, user_id: str) -> None:
        if user_id in self._failing_user_ids:
            raise bulk_delete_accounts.BulkAccountDeletionError(f"core rejected {user_id}")

    def revoke_all_sessions(self, user_id: str) -> int:
        self._fail_if_configured(user_id)
        self.revoked_user_ids.append(user_id)
        return self._revoked_sessions_per_user

    def remove_user(self, user_id: str) -> None:
        self._fail_if_configured(user_id)
        self.removed_user_ids.append(user_id)


def _core_client_with_mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> Any:
    """A ``SupertokensCoreClient`` whose HTTP layer is a MockTransport, with the real headers preserved."""
    client = bulk_delete_accounts.SupertokensCoreClient(
        core_uri="http://core.test", api_key="test-api-key", max_connections=2
    )
    original_headers = dict(client._client.headers)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), headers=original_headers)
    return client


def _post_without_retries(client: Any, path: str) -> dict[str, Any]:
    """Call ``_post`` via ``__wrapped__`` so the tenacity backoff never sleeps in tests."""
    return client._post.__wrapped__(client, path, {})


def test_remove_user_posts_user_id_with_pinned_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "OK"})

    client = _core_client_with_mock_transport(handler)
    try:
        client.remove_user("e055efda-494d-4b0d-90e6-2eb4e0d4949b")
    finally:
        client.close()
    (request,) = requests
    assert str(request.url) == "http://core.test/user/remove"
    assert json.loads(request.content) == {"userId": "e055efda-494d-4b0d-90e6-2eb4e0d4949b"}
    assert request.headers["api-key"] == "test-api-key"
    assert request.headers["cdi-version"] == bulk_delete_accounts._SUPERTOKENS_CDI_VERSION


def test_remove_user_raises_on_non_ok_core_status() -> None:
    client = _core_client_with_mock_transport(
        lambda request: httpx.Response(200, json={"status": "UNKNOWN_USER_ID_ERROR"})
    )
    try:
        with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError, match="UNKNOWN_USER_ID_ERROR"):
            client.remove_user("e055efda-494d-4b0d-90e6-2eb4e0d4949b")
    finally:
        client.close()


def test_revoke_all_sessions_sends_both_revoke_flags_and_counts_handles() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "OK", "sessionHandlesRevoked": ["s1", "s2"]})

    client = _core_client_with_mock_transport(handler)
    try:
        revoked = client.revoke_all_sessions("e055efda-494d-4b0d-90e6-2eb4e0d4949b")
    finally:
        client.close()
    assert revoked == 2
    (request,) = requests
    assert str(request.url) == "http://core.test/recipe/session/remove"
    assert json.loads(request.content) == {
        "userId": "e055efda-494d-4b0d-90e6-2eb4e0d4949b",
        "revokeAcrossAllTenants": True,
        "revokeSessionsForLinkedAccounts": True,
    }


@pytest.mark.parametrize("status_code", (429, 500, 503))
def test_post_classifies_throttling_and_server_errors_as_retryable(status_code: int) -> None:
    client = _core_client_with_mock_transport(lambda request: httpx.Response(status_code))
    try:
        with pytest.raises(bulk_delete_accounts.RetryableCoreError):
            _post_without_retries(client, "/user/remove")
    finally:
        client.close()


def test_post_treats_client_errors_as_fatal_not_retryable() -> None:
    client = _core_client_with_mock_transport(lambda request: httpx.Response(400, text="bad request"))
    try:
        with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError, match="400") as exc_info:
            _post_without_retries(client, "/user/remove")
    finally:
        client.close()
    assert not isinstance(exc_info.value, bulk_delete_accounts.RetryableCoreError)


def test_post_wraps_non_json_body_in_tool_error() -> None:
    client = _core_client_with_mock_transport(lambda request: httpx.Response(200, text="<html>gateway</html>"))
    try:
        with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError, match="non-JSON"):
            _post_without_retries(client, "/user/remove")
    finally:
        client.close()


def test_share_label_strips_hyphens_and_lowercases() -> None:
    label = bulk_delete_accounts._share_label_for_user_id("E055EFDA-494D-4B0D-90E6-2EB4E0D4949B")
    assert label == "e055efda494d4b0d90e62eb4e0d4949b"


def test_user_id_prefix_is_first_sixteen_hex_chars() -> None:
    prefix = bulk_delete_accounts._user_id_prefix("e055efda-494d-4b0d-90e6-2eb4e0d4949b")
    assert prefix == "e055efda494d4b0d"


def test_load_accounts_reads_user_ids_and_ignores_email_column(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text(
        "email,user_id\n"
        "a@imbue.com,11111111-1111-1111-1111-111111111111\n"
        "b@imbue.com,22222222-2222-2222-2222-222222222222\n"
    )
    user_ids = bulk_delete_accounts._load_accounts(accounts_file)
    assert user_ids == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]


def test_load_accounts_lowercases_user_ids(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text("email,user_id\na@imbue.com,E055EFDA-494D-4B0D-90E6-2EB4E0D4949B\n")
    user_ids = bulk_delete_accounts._load_accounts(accounts_file)
    assert user_ids == ["e055efda-494d-4b0d-90e6-2eb4e0d4949b"]


def test_load_accounts_rejects_case_variant_duplicate_user_ids(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text(
        "email,user_id\n"
        "a@imbue.com,e055efda-494d-4b0d-90e6-2eb4e0d4949b\n"
        "b@imbue.com,E055EFDA-494D-4B0D-90E6-2EB4E0D4949B\n"
    )
    with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError):
        bulk_delete_accounts._load_accounts(accounts_file)


def test_load_accounts_rejects_duplicate_user_ids(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text(
        "email,user_id\n"
        "a@imbue.com,11111111-1111-1111-1111-111111111111\n"
        "b@imbue.com,11111111-1111-1111-1111-111111111111\n"
    )
    with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError):
        bulk_delete_accounts._load_accounts(accounts_file)


def test_load_accounts_rejects_non_uuid_user_ids(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text('email,user_id\n"a@imbue.com","1111\tgarbage"\n')
    with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError):
        bulk_delete_accounts._load_accounts(accounts_file)


def test_load_accounts_rejects_unhyphenated_user_ids(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text("email,user_id\na@imbue.com,e055efda494d4b0d90e62eb4e0d4949b\n")
    with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError):
        bulk_delete_accounts._load_accounts(accounts_file)


def test_load_accounts_skips_blank_user_id_rows(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text("email,user_id\nkeep@imbue.com,33333333-3333-3333-3333-333333333333\n,\n")
    user_ids = bulk_delete_accounts._load_accounts(accounts_file)
    assert user_ids == ["33333333-3333-3333-3333-333333333333"]


def test_load_accounts_requires_user_id_column(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text("email\nonly@imbue.com\n")
    with pytest.raises(bulk_delete_accounts.BulkAccountDeletionError):
        bulk_delete_accounts._load_accounts(accounts_file)


def test_load_completed_user_ids_filters_by_phase_tier_and_success(tmp_path: Path) -> None:
    progress_file = tmp_path / "progress.jsonl"
    progress_file.write_text(
        json.dumps({"user_id": "u1", "phase": "DELETE", "tier": "production", "ok": True})
        + "\n"
        + json.dumps({"user_id": "u2", "phase": "DELETE", "tier": "production", "ok": False, "error": "boom"})
        + "\n"
        + json.dumps({"user_id": "u3", "phase": "REVOKE", "tier": "production", "ok": True})
        + "\n"
        # Completed on another tier: the same accounts CSV (and thus the same
        # default progress file) run against production must not skip it.
        + json.dumps({"user_id": "u4", "phase": "DELETE", "tier": "staging", "ok": True})
        + "\n"
        + json.dumps({"phase": "DELETE", "tier": "production", "ok": True})
        + "\n"
        + "not-json\n"
    )
    completed = bulk_delete_accounts._load_completed_user_ids(
        progress_file, bulk_delete_accounts.TakedownPhase.DELETE, "production"
    )
    assert completed == {"u1"}


def test_load_completed_user_ids_returns_empty_for_missing_file(tmp_path: Path) -> None:
    completed = bulk_delete_accounts._load_completed_user_ids(
        tmp_path / "absent.jsonl", bulk_delete_accounts.TakedownPhase.REVOKE, "production"
    )
    assert completed == set()


def test_delete_db_rows_touches_only_existing_tables() -> None:
    cursor = _RecordingCursor(rowcount=2)
    existing_tables = {"account_entitlements", "workspace_records"}
    counts = bulk_delete_accounts._delete_db_rows_for_listed_accounts(_RecordingConnection(cursor), existing_tables)
    assert set(counts) == existing_tables
    assert counts == {"account_entitlements": 2, "workspace_records": 2}
    assert all("DELETE FROM" in sql for sql in cursor.executed)
    assert not any("shares" in sql for sql in cursor.executed)


def test_delete_db_rows_keys_share_tables_by_share_label_column() -> None:
    cursor = _RecordingCursor(rowcount=0)
    all_tables = set(bulk_delete_accounts._TABLES_KEYED_BY_USER_ID) | set(
        bulk_delete_accounts._TABLES_KEYED_BY_SHARE_LABEL
    )
    bulk_delete_accounts._delete_db_rows_for_listed_accounts(_RecordingConnection(cursor), all_tables)
    share_statements = [sql for sql in cursor.executed if "FROM shares" in sql or "FROM relay_tokens" in sql]
    assert len(share_statements) == 2
    # relay_tokens must be deleted before shares: its ON DELETE CASCADE FK onto
    # shares would otherwise empty it first and zero its reported count.
    assert "FROM relay_tokens" in share_statements[0]
    assert "FROM shares" in share_statements[1]
    assert all("a.user_id = t.share_label" in sql for sql in share_statements)
    user_id_statements = [sql for sql in cursor.executed if sql not in share_statements]
    assert all("a.user_id = t.user_id" in sql for sql in user_id_statements)


def test_count_db_rows_touches_only_existing_tables_with_select_statements() -> None:
    cursor = _RecordingCursor(rowcount=3)
    existing_tables = {"account_entitlements", "workspace_records"}
    counts = bulk_delete_accounts._count_db_rows_for_listed_accounts(_RecordingConnection(cursor), existing_tables)
    assert counts == {"account_entitlements": 3, "workspace_records": 3}
    assert all(sql.startswith("SELECT COUNT(*)") for sql in cursor.executed)
    assert not any("DELETE" in sql for sql in cursor.executed)
    assert not any("shares" in sql for sql in cursor.executed)


def test_count_db_rows_keys_share_tables_by_share_label_column() -> None:
    cursor = _RecordingCursor(rowcount=0)
    all_tables = set(bulk_delete_accounts._TABLES_KEYED_BY_USER_ID) | set(
        bulk_delete_accounts._TABLES_KEYED_BY_SHARE_LABEL
    )
    bulk_delete_accounts._count_db_rows_for_listed_accounts(_RecordingConnection(cursor), all_tables)
    share_statements = [sql for sql in cursor.executed if "FROM shares" in sql or "FROM relay_tokens" in sql]
    assert len(share_statements) == 2
    assert all("a.user_id = t.share_label" in sql for sql in share_statements)
    user_id_statements = [sql for sql in cursor.executed if sql not in share_statements]
    assert len(user_id_statements) == len(bulk_delete_accounts._TABLES_KEYED_BY_USER_ID)
    assert all("a.user_id = t.user_id" in sql for sql in user_id_statements)


def test_count_held_pool_hosts_matches_on_lease_prefix_alone_with_no_status_filter() -> None:
    cursor = _RecordingCursor(rowcount=4)
    held_count = bulk_delete_accounts._count_held_pool_hosts_for_listed_accounts(_RecordingConnection(cursor))
    assert held_count == 4
    (sql,) = cursor.executed
    assert "p.leased_to_user = t.lease_prefix" in sql
    # Fail-safe by design: the guard must NOT filter on status. A released host
    # leaves no row, so any surviving row naming the user means the release did
    # not complete, in any lifecycle status. A status filter here would silently
    # under-match a newly-added held status and let a delete orphan a live VM.
    assert "status" not in sql.lower()


def test_copy_temp_table_rows_carry_derived_share_label_and_lease_prefix() -> None:
    cursor = _RecordingCursor(rowcount=0)
    bulk_delete_accounts._copy_user_ids_into_temp_table(
        _RecordingConnection(cursor), ["E055EFDA-494D-4B0D-90E6-2EB4E0D4949B"]
    )
    assert len(cursor.copied) == 1
    copy_sql, copied_rows = cursor.copied[0]
    assert "(user_id, share_label, lease_prefix)" in copy_sql
    assert copied_rows == (
        "E055EFDA-494D-4B0D-90E6-2EB4E0D4949B\te055efda494d4b0d90e62eb4e0d4949b\tE055EFDA494D4B0D\n"
    )


def test_phase_runner_records_delete_outcomes_and_resume_skips_only_successes(tmp_path: Path) -> None:
    progress_file = tmp_path / "progress.jsonl"
    core_client = _FakeCoreClient(failing_user_ids={"u2"}, revoked_sessions_per_user=0)
    runner = bulk_delete_accounts._PhaseRunner(
        core_client=core_client,
        phase=bulk_delete_accounts.TakedownPhase.DELETE,
        tier="production",
        progress_file=progress_file,
        worker_count=2,
    )
    completed_count, failed_count, revoked_session_count = runner.run(["u1", "u2", "u3"])
    assert (completed_count, failed_count, revoked_session_count) == (2, 1, 0)
    assert set(core_client.removed_user_ids) == {"u1", "u3"}
    records_by_user = {record["user_id"]: record for record in map(json.loads, progress_file.read_text().splitlines())}
    assert len(records_by_user) == 3
    assert all(record["phase"] == "DELETE" and record["tier"] == "production" for record in records_by_user.values())
    assert records_by_user["u1"]["ok"] is True and "error" not in records_by_user["u1"]
    assert records_by_user["u2"]["ok"] is False and "core rejected u2" in records_by_user["u2"]["error"]
    completed = bulk_delete_accounts._load_completed_user_ids(
        progress_file, bulk_delete_accounts.TakedownPhase.DELETE, "production"
    )
    assert completed == {"u1", "u3"}


def test_phase_runner_revoke_sums_revoked_sessions_across_users(tmp_path: Path) -> None:
    core_client = _FakeCoreClient(failing_user_ids=set(), revoked_sessions_per_user=2)
    runner = bulk_delete_accounts._PhaseRunner(
        core_client=core_client,
        phase=bulk_delete_accounts.TakedownPhase.REVOKE,
        tier="production",
        progress_file=tmp_path / "progress.jsonl",
        worker_count=2,
    )
    completed_count, failed_count, revoked_session_count = runner.run(["u1", "u2", "u3"])
    assert (completed_count, failed_count, revoked_session_count) == (3, 0, 6)
    assert set(core_client.revoked_user_ids) == {"u1", "u2", "u3"}
    assert core_client.removed_user_ids == []
