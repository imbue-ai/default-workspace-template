"""Unit tests for the local ``delete_accounts.py`` operator tool.

The script is not an importable package module (it lives under ``scripts/``), so
it is loaded from its path. Only the pure, side-effect-free helpers are covered
here; the live SuperTokens / Neon / LiteLLM I/O is exercised operationally via
the tool's ``--dry-run`` default, not in unit tests.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_delete_accounts_module() -> ModuleType:
    module_path = Path(__file__).parent / "delete_accounts.py"
    spec = importlib.util.spec_from_file_location("delete_accounts_under_test", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


delete_accounts_module = _load_delete_accounts_module()


class _RecordingCursor:
    """Fake psycopg2 cursor that records executed statements and reports a fixed rowcount."""

    def __init__(self, rowcount_by_table: dict[str, int]) -> None:
        self._rowcount_by_table = rowcount_by_table
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.rowcount = 0

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))
        table = sql.split("FROM ")[1].split(" ")[0]
        self.rowcount = self._rowcount_by_table.get(table, 0)


class _RecordingConnection:
    """Fake psycopg2 connection handing out a single recording cursor."""

    def __init__(self, cursor: _RecordingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _RecordingCursor:
        return self._cursor


def test_share_label_strips_hyphens_and_lowercases() -> None:
    label = delete_accounts_module._share_label_for_user_id("E055EFDA-494D-4B0D-90E6-2EB4E0D4949B")
    assert label == "e055efda494d4b0d90e62eb4e0d4949b"
    assert "-" not in label


def test_load_accounts_reads_email_and_user_id_columns(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text(
        "email,user_id,user_id_prefix\n"
        "a@imbue.com,11111111-1111-1111-1111-111111111111,1111\n"
        "b@imbue.com,22222222-2222-2222-2222-222222222222,2222\n"
    )
    accounts = delete_accounts_module._load_accounts(accounts_file)
    assert accounts == [
        ("a@imbue.com", "11111111-1111-1111-1111-111111111111"),
        ("b@imbue.com", "22222222-2222-2222-2222-222222222222"),
    ]


def test_load_accounts_skips_blank_user_id_rows(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text("email,user_id\nkeep@imbue.com,33333333-3333-3333-3333-333333333333\n,\n")
    accounts = delete_accounts_module._load_accounts(accounts_file)
    assert accounts == [("keep@imbue.com", "33333333-3333-3333-3333-333333333333")]


def test_load_accounts_requires_user_id_column(tmp_path: Path) -> None:
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text("email\nonly@imbue.com\n")
    with pytest.raises(delete_accounts_module.AccountDeletionError):
        delete_accounts_module._load_accounts(accounts_file)


def test_resolve_secret_prefers_explicit_then_env_then_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_SECRET_ENV", "from-env")
    local_values = {"SOME_SECRET_ENV": "from-local"}
    assert (
        delete_accounts_module._resolve_secret("explicit", "SOME_SECRET_ENV", "production", "svc", "K", local_values)
        == "explicit"
    )
    assert (
        delete_accounts_module._resolve_secret(None, "SOME_SECRET_ENV", "production", "svc", "K", local_values)
        == "from-env"
    )
    monkeypatch.delenv("SOME_SECRET_ENV")
    assert (
        delete_accounts_module._resolve_secret(None, "SOME_SECRET_ENV", "production", "svc", "K", local_values)
        == "from-local"
    )


def test_load_env_local_secrets_reads_the_secrets_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    env_dir = tmp_path / ".minds-dev-alice"
    env_dir.mkdir()
    (env_dir / "secrets.toml").write_text('[secrets]\nNEON_HOST_POOL_DSN = "postgresql://local/host_pool"\n')

    values = delete_accounts_module._load_env_local_secrets("dev-alice")

    assert values == {"NEON_HOST_POOL_DSN": "postgresql://local/host_pool"}
    # No env named: nothing to read, no error.
    assert delete_accounts_module._load_env_local_secrets(None) == {}


def test_load_env_local_secrets_fails_loudly_on_missing_or_malformed_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(delete_accounts_module.AccountDeletionError, match="does not exist"):
        delete_accounts_module._load_env_local_secrets("dev-alice")

    env_dir = tmp_path / ".minds-dev-alice"
    env_dir.mkdir()
    (env_dir / "secrets.toml").write_text('secrets = "not-a-table"\n')
    with pytest.raises(delete_accounts_module.AccountDeletionError, match="not a table"):
        delete_accounts_module._load_env_local_secrets("dev-alice")


def test_resolve_optional_secret_returns_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIONAL_SECRET_ENV", "opt-value")
    assert (
        delete_accounts_module._resolve_optional_secret(None, "OPTIONAL_SECRET_ENV", "production", "svc", "K", {})
        == "opt-value"
    )


def test_resolve_analytics_secret_values_skips_only_a_fully_absent_entry() -> None:
    """No analytics keys at all means 'not provisioned' (skip); all keys means proceed."""
    assert delete_accounts_module._resolve_analytics_secret_values("production", lambda key: None) is None

    values = delete_accounts_module._resolve_analytics_secret_values("production", lambda key: f"value-for-{key}")
    assert values is not None
    assert set(values) == set(delete_accounts_module._ANALYTICS_SECRET_KEYS)
    assert values["ANALYTICS_OPS_DATABASE_URL"] == "value-for-ANALYTICS_OPS_DATABASE_URL"


def test_resolve_analytics_secret_values_refuses_a_partially_populated_entry() -> None:
    """A misconfigured (partial) analytics entry must abort, never silently skip deletion."""

    def resolve_all_but_ops(key: str) -> str | None:
        return None if key == "ANALYTICS_OPS_DATABASE_URL" else f"value-for-{key}"

    with pytest.raises(delete_accounts_module.AccountDeletionError, match="ANALYTICS_OPS_DATABASE_URL"):
        delete_accounts_module._resolve_analytics_secret_values("production", resolve_all_but_ops)


def test_delete_db_rows_keys_each_table_by_the_correct_identifier() -> None:
    """The full user id keys most tables; shares/relay_tokens key on the 32-hex share label."""
    user_id = "e055efda-494d-4b0d-90e6-2eb4e0d4949b"
    expected_label = "e055efda494d4b0d90e62eb4e0d4949b"
    cursor = _RecordingCursor(
        rowcount_by_table={
            "account_entitlements": 1,
            "workspace_records": 3,
            "account_key_bundles": 0,
            "r2_cleanup_grants": 0,
            "shares": 0,
            "relay_tokens": 0,
        }
    )
    all_tables = {
        "account_entitlements",
        "workspace_records",
        "account_key_bundles",
        "r2_cleanup_grants",
        "shares",
        "relay_tokens",
    }
    counts = delete_accounts_module._delete_db_rows_for_user(_RecordingConnection(cursor), user_id, all_tables)

    key_by_table = {sql.split("FROM ")[1].split(" ")[0]: params[0] for sql, params in cursor.executed}
    assert key_by_table["account_entitlements"] == user_id
    assert key_by_table["workspace_records"] == user_id
    assert key_by_table["account_key_bundles"] == user_id
    assert key_by_table["r2_cleanup_grants"] == user_id
    assert key_by_table["shares"] == expected_label
    assert key_by_table["relay_tokens"] == expected_label
    assert counts == {
        "account_entitlements": 1,
        "workspace_records": 3,
        "account_key_bundles": 0,
        "r2_cleanup_grants": 0,
        "shares": 0,
        "relay_tokens": 0,
    }


def test_delete_db_rows_skips_tables_absent_from_the_database() -> None:
    """A table missing from this DB (e.g. sharing not provisioned) is skipped, not queried."""
    user_id = "e055efda-494d-4b0d-90e6-2eb4e0d4949b"
    cursor = _RecordingCursor(rowcount_by_table={"account_entitlements": 1, "workspace_records": 2})
    existing_tables = {"account_entitlements", "workspace_records", "account_key_bundles", "r2_cleanup_grants"}
    counts = delete_accounts_module._delete_db_rows_for_user(_RecordingConnection(cursor), user_id, existing_tables)

    queried_tables = {sql.split("FROM ")[1].split(" ")[0] for sql, _ in cursor.executed}
    assert "shares" not in queried_tables
    assert "relay_tokens" not in queried_tables
    assert "shares" not in counts
    assert "relay_tokens" not in counts
    assert counts["account_entitlements"] == 1
    assert counts["workspace_records"] == 2


class _CountingCursor:
    """Fake psycopg2 cursor whose fetchone reports a fixed count, recording the SQL run."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> "_CountingCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[int]:
        return (self._count,)


class _CountingConnection:
    """Fake psycopg2 connection handing out a single counting cursor."""

    def __init__(self, cursor: _CountingCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _CountingCursor:
        return self._cursor


def test_count_leased_hosts_matches_on_prefix_alone_with_no_status_filter() -> None:
    """The lease guard must match every row naming the user, in any status (fail-safe)."""
    cursor = _CountingCursor(count=3)
    held = delete_accounts_module._count_leased_hosts_for_user(
        _CountingConnection(cursor), "e055efda-494d-4b0d-90e6-2eb4e0d4949b"
    )
    assert held == 3
    (sql, params) = cursor.executed[0]
    assert params == ("e055efda494d4b0d",)
    # No status filter: a released host leaves no row, so any surviving row means
    # an incomplete release. Filtering on status would under-match newly-added
    # held statuses and let a delete strand a live VM.
    assert "status" not in sql.lower()
