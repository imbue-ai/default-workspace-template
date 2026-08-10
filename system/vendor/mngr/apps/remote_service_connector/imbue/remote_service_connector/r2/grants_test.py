from pathlib import Path

import imbue.remote_service_connector.r2.stores as r2_stores_mod


def test_cleanup_grants_migration_matches_grant_columns() -> None:
    """Guard against the r2_cleanup_grants schema and the store's column list drifting apart."""
    migrations_dir = Path(__file__).parent.parent.parent.parent / "migrations"
    migration_sql = (migrations_dir / "015_r2_cleanup_grants.sql").read_text().lower()
    rename_sql = (migrations_dir / "016_rename_username_prefix.sql").read_text().lower()
    assert "create table r2_cleanup_grants" in migration_sql
    assert "alter table r2_cleanup_grants rename column username_prefix to user_id_prefix" in rename_sql
    # The effective schema is the create-table migration with the rename applied.
    effective_sql = migration_sql.replace("username_prefix", "user_id_prefix")
    for column in (name.strip() for name in r2_stores_mod._R2_GRANT_COLUMNS.split(",")):
        assert column in effective_sql, f"grant column {column!r} missing from the migration"
