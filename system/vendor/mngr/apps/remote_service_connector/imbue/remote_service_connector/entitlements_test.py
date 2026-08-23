from pathlib import Path

import pytest

import imbue.remote_service_connector.entitlements as entitlements_mod
import imbue.remote_service_connector.errors as errors_mod
from imbue.remote_service_connector.testing import EXPLORER_PLAN_VALUES
from imbue.remote_service_connector.testing import InMemoryEntitlementsStore
from imbue.remote_service_connector.testing import make_fake_entitlements_store


def test_initial_plan_pre_cutoff_paid_email_gets_ally() -> None:
    plan = entitlements_mod._initial_plan_name_for_user(
        "user-1", "alice@imbue.com", time_joined_getter=lambda uid: 0, paid_checker=lambda email: True
    )
    assert plan == "ally"


def test_initial_plan_post_cutoff_paid_email_gets_explorer() -> None:
    """Accounts created after the ship cutoff always start as explorer, paid-listed or not."""
    after_cutoff = entitlements_mod._PREEXISTING_ACCOUNT_CUTOFF_EPOCH_MS + 1
    plan = entitlements_mod._initial_plan_name_for_user(
        "user-1", "alice@imbue.com", time_joined_getter=lambda uid: after_cutoff, paid_checker=lambda email: True
    )
    assert plan == "explorer"


def test_initial_plan_unpaid_email_gets_explorer() -> None:
    plan = entitlements_mod._initial_plan_name_for_user(
        "user-1", "bob@gmail.com", time_joined_getter=lambda uid: 0, paid_checker=lambda email: False
    )
    assert plan == "explorer"


def test_ensure_account_entitlements_copies_plan_values_and_is_idempotent() -> None:
    store = make_fake_entitlements_store()
    first = entitlements_mod.ensure_account_entitlements(
        user_id="user-1", user_id_prefix="prefix1", email="", store=store
    )
    assert first.plan_name == "explorer"
    assert first.max_remote_workspaces == EXPLORER_PLAN_VALUES["max_remote_workspaces"]
    # A manual bump survives a second ensure (lazy creation never overwrites).
    store.update_entitlements("user-1", {"max_remote_workspaces": 7})
    second = entitlements_mod.ensure_account_entitlements(
        user_id="user-1", user_id_prefix="prefix1", email="", store=store
    )
    assert second.max_remote_workspaces == 7


def test_ensure_account_entitlements_raises_when_plan_not_seeded() -> None:
    store = InMemoryEntitlementsStore()
    with pytest.raises(errors_mod.PlanNotFoundError):
        entitlements_mod.ensure_account_entitlements(user_id="user-1", user_id_prefix="p", email="", store=store)


def test_plans_migration_declares_all_quota_columns() -> None:
    """Guard against the plans/entitlements schema and QUOTA_ENTITLEMENT_NAMES drifting apart."""
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    # Quota columns are declared by 014 (the original tables) plus any later
    # ALTERs that add entitlements (024 added max_total_workspaces); the drift
    # guard checks their union.
    migration_sql = (migrations_dir / "014_plans_entitlements.sql").read_text().lower() + (
        migrations_dir / "024_workspace_stop_start.sql"
    ).read_text().lower()
    rename_sql = (migrations_dir / "016_rename_username_prefix.sql").read_text().lower()
    assert "create table plans" in migration_sql
    assert "create table account_entitlements" in migration_sql
    # Migration 014 created the column under its old name; 016 renames it to
    # match the code's user_id_prefix.
    assert "username_prefix" in migration_sql
    assert "alter table account_entitlements rename column username_prefix to user_id_prefix" in rename_sql
    assert "enforced_access" in migration_sql
    for column in entitlements_mod.QUOTA_ENTITLEMENT_NAMES:
        assert migration_sql.count(column) >= 2, f"quota column {column!r} missing from a table"
