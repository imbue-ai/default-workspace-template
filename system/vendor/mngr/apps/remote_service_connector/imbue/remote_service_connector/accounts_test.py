from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import UUID

import pytest
from starlette.testclient import TestClient

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.testing import ALLY_PLAN_VALUES
from imbue.remote_service_connector.testing import EXPLORER_PLAN_VALUES
from imbue.remote_service_connector.testing import FakeLiteLLMBackend
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import InMemoryEntitlementsStore
from imbue.remote_service_connector.testing import _ADMIN_KEY_TEST_VALUE
from imbue.remote_service_connector.testing import _USER_STUB_EMAIL
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_bucket_quota_test_client
from imbue.remote_service_connector.testing import _make_bucket_test_client
from imbue.remote_service_connector.testing import _make_paid_crud_test_client
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _make_quota_test_client
from imbue.remote_service_connector.testing import _make_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _user_headers
from imbue.remote_service_connector.testing import make_fake_pool_backend
from imbue.remote_service_connector.testing import make_fake_supertokens_backend


def test_paid_crud_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # No Authorization header.
    assert client.get("/paid/domains").status_code == 401
    # Wrong key.
    bad = client.get("/paid/domains", headers={"Authorization": "Bearer wrong-key"})
    assert bad.status_code == 401
    # A SuperTokens user JWT is NOT accepted on the paid CRUD endpoints.
    assert client.get("/paid/domains", headers=_user_headers()).status_code == 401


def test_paid_crud_rejects_non_ascii_bearer_token_with_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-ASCII bearer credential is a clean 401, not a 500 (compare over bytes)."""
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    # HTTP header values are latin-1; pass raw bytes (both key and value, which
    # matches httpx's ``Mapping[bytes, bytes]`` header type) so the non-ASCII
    # octets reach the handler -- httpx would otherwise reject a non-ASCII str.
    resp = client.get("/paid/domains", headers={b"Authorization": "Bearer wröng-kéy".encode("latin-1")})
    assert resp.status_code == 401


def test_paid_crud_returns_403_when_admin_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_pool_test_client(monkeypatch)
    monkeypatch.delenv("MINDS_ADMIN_KEY", raising=False)
    monkeypatch.delenv("MINDS_PAID_ADMIN_KEY", raising=False)
    resp = client.get("/paid/domains", headers=_admin_key_headers())
    assert resp.status_code == 403
    assert "not enabled" in resp.json()["detail"]


def test_add_and_list_paid_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    add_resp = client.post("/paid/domains/add", json={"value": "Imbue.com"}, headers=_admin_key_headers())
    assert add_resp.status_code == 200
    assert add_resp.json() == {"status": "added", "domain": "imbue.com"}
    list_resp = client.get("/paid/domains", headers=_admin_key_headers())
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert [r["domain"] for r in rows] == ["imbue.com"]
    assert rows[0]["is_paid"] is True


def test_remove_paid_domain_is_soft_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_admin_key_headers())
    remove_resp = client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_admin_key_headers())
    assert remove_resp.status_code == 200
    # The row is still present (soft delete), but is_paid is now false.
    all_rows = client.get("/paid/domains", headers=_admin_key_headers()).json()
    assert [(r["domain"], r["is_paid"]) for r in all_rows] == [("imbue.com", False)]
    # paid_only filter hides it.
    paid_rows = client.get("/paid/domains?paid_only=true", headers=_admin_key_headers()).json()
    assert paid_rows == []


def test_re_adding_soft_removed_domain_reactivates_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_admin_key_headers())
    original = client.get("/paid/domains", headers=_admin_key_headers()).json()[0]
    client.post("/paid/domains/remove", json={"value": "imbue.com"}, headers=_admin_key_headers())
    client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_admin_key_headers())
    reactivated = client.get("/paid/domains", headers=_admin_key_headers()).json()[0]
    assert reactivated["is_paid"] is True
    # created_at is preserved across the remove/re-add cycle.
    assert reactivated["created_at"] == original["created_at"]


def test_add_paid_domain_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    first = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_admin_key_headers())
    second = client.post("/paid/domains/add", json={"value": "imbue.com"}, headers=_admin_key_headers())
    assert first.status_code == 200
    assert second.status_code == 200
    rows = client.get("/paid/domains", headers=_admin_key_headers()).json()
    assert len(rows) == 1


def test_remove_absent_paid_email_is_idempotent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/remove", json={"value": "nobody@nowhere.com"}, headers=_admin_key_headers())
    assert resp.status_code == 200
    assert resp.json() == {"status": "removed", "email": "nobody@nowhere.com"}


def test_add_paid_email_then_ally_plan_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: adding a paid email via CRUD makes the ally plan selectable."""
    client, backend = _make_paid_crud_test_client(monkeypatch)
    # Start from a clean slate where the stub email is not paid.
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    denied = client.post("/account/plan", json={"plan": "ally"}, headers=_user_headers())
    assert denied.status_code == 403
    assert "partner access" in denied.json()["detail"]
    client.post("/paid/emails/add", json={"value": _USER_STUB_EMAIL}, headers=_admin_key_headers())
    allowed = client.post("/account/plan", json={"plan": "ally"}, headers=_user_headers())
    assert allowed.status_code == 200
    assert allowed.json()["plan_name"] == "ally"


def test_add_paid_email_never_marks_the_account_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding an email to the paid list must NOT verify a pre-existing account for it.

    Verification is proof of mailbox ownership; paid-listing an email says
    nothing about who owns the account that claimed it. (The old auto-verify
    existed only to un-block paid users from the then-global verified gate.)
    """
    client, _pool_backend = _make_paid_crud_test_client(monkeypatch)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    # A user who signed up earlier but never verified their email.
    st_backend.sign_up(tenant_id="public", email="waiting@example.com", password="password123")
    assert st_backend.accounts_by_email["waiting@example.com"].is_verified is False

    resp = client.post("/paid/emails/add", json={"value": "waiting@example.com"}, headers=_admin_key_headers())

    assert resp.status_code == 200
    assert st_backend.accounts_by_email["waiting@example.com"].is_verified is False


def test_add_paid_email_with_no_existing_account_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding a paid email with no matching account succeeds (a plain list write)."""
    client, _pool_backend = _make_paid_crud_test_client(monkeypatch)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)

    resp = client.post("/paid/emails/add", json={"value": "nobody@example.com"}, headers=_admin_key_headers())

    assert resp.status_code == 200
    assert resp.json() == {"status": "added", "email": "nobody@example.com"}
    assert "nobody@example.com" not in st_backend.accounts_by_email


def test_add_paid_email_succeeds_when_supertokens_uninitialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SuperTokens outage during the auto-verify side effect must not fail the paid-list write.

    No SuperTokens fake is installed, so the real (uninitialized) SDK raises when
    the handler tries to look the account up; that error must be swallowed and
    the paid-list add must still succeed.
    """
    client, _pool_backend = _make_paid_crud_test_client(monkeypatch)

    resp = client.post("/paid/emails/add", json={"value": "someone@example.com"}, headers=_admin_key_headers())

    assert resp.status_code == 200
    assert resp.json() == {"status": "added", "email": "someone@example.com"}


@pytest.mark.parametrize("bad_value", ["", "   ", "has space", "foo@bar.com"])
def test_add_paid_domain_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/domains/add", json={"value": bad_value}, headers=_admin_key_headers())
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_value", ["", "no-at-sign", "@nodomain", "local@", "a b@c.com"])
def test_add_paid_email_rejects_invalid(monkeypatch: pytest.MonkeyPatch, bad_value: str) -> None:
    client, _backend = _make_paid_crud_test_client(monkeypatch)
    resp = client.post("/paid/emails/add", json={"value": bad_value}, headers=_admin_key_headers())
    assert resp.status_code == 400


def test_paid_lists_migration_declares_both_tables() -> None:
    """Guard against the paid_domains / paid_emails schema drifting from the gate queries."""
    migration_path = Path(__file__).parent.parent.parent / "migrations" / "005_paid_lists.sql"
    migration_sql = migration_path.read_text().lower()
    assert "create table paid_domains" in migration_sql
    assert "create table paid_emails" in migration_sql
    for column in ("is_paid", "created_at", "updated_at"):
        assert column in migration_sql, f"paid-lists migration is missing column {column!r}"


def test_route_set_account_plan_same_plan_is_noop_preserving_bumps(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "ally", max_remote_workspaces=42)
    resp = client.post("/account/plan", json={"plan": "ally"}, headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["entitlements"]["max_remote_workspaces"] == 42
    # No LiteLLM push on a no-op.
    assert litellm.calls == []


def test_route_set_account_plan_switch_overwrites_wholesale(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_remote_workspaces=42)
    resp = client.post("/account/plan", json={"plan": "ally"}, headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_name"] == "ally"
    # The manual bump is wiped: values reset wholesale to the plan defaults.
    assert body["entitlements"]["max_remote_workspaces"] == ALLY_PLAN_VALUES["max_remote_workspaces"]
    # The new monthly budget is pushed to LiteLLM.
    assert litellm.users_by_id[_USER_STUB_USER_ID]["max_budget"] == ALLY_PLAN_VALUES["monthly_llm_spend_usd"]


def test_route_set_account_plan_unknown_plan_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post("/account/plan", json={"plan": "platinum"}, headers=_user_headers())
    assert resp.status_code == 400


def test_route_set_account_plan_litellm_failure_aborts_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed LiteLLM budget push fails the whole switch; the row is unchanged."""
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer")
    litellm.fail_user_writes = True
    resp = client.post("/account/plan", json={"plan": "ally"}, headers=_user_headers())
    assert resp.status_code == 500
    row = entitlements_store.get_entitlements(_USER_STUB_USER_ID)
    assert row is not None
    assert row["plan_name"] == "explorer"


def _make_account_admin_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemoryEntitlementsStore, FakeLiteLLMBackend, FakeSuperTokensBackend]:
    client, entitlements_store, litellm = _make_quota_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    return client, entitlements_store, litellm, st_backend


def test_admin_get_account_lazily_creates_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    resp = client.get("/admin/accounts/somebody@example.com", headers=_admin_key_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "somebody@example.com"
    # A brand-new account with no explicit signup choice backfills as free.
    assert body["plan_name"] == "free"
    assert entitlements_store.get_entitlements(body["user_id"]) is not None


def test_admin_get_account_unknown_email_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm, _st_backend = _make_account_admin_test_client(monkeypatch)
    resp = client.get("/admin/accounts/nobody@example.com", headers=_admin_key_headers())
    assert resp.status_code == 404


def test_admin_account_endpoints_reject_missing_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    # A SuperTokens session token is rejected on the admin-key routes.
    resp = client.get("/admin/accounts/somebody@example.com", headers=_user_headers())
    assert resp.status_code == 401


def test_admin_set_plan_always_resets_to_plan_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admin set-plan resets even for the same plan (the operator's bump-wipe)."""
    client, entitlements_store, litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    show = client.get("/admin/accounts/somebody@example.com", headers=_admin_key_headers()).json()
    entitlements_store.update_entitlements(show["user_id"], {"max_remote_workspaces": 42})
    resp = client.post(
        "/admin/accounts/somebody@example.com/plan", json={"plan": "explorer"}, headers=_admin_key_headers()
    )
    assert resp.status_code == 200
    row = entitlements_store.get_entitlements(show["user_id"])
    assert row is not None
    assert row["max_remote_workspaces"] == EXPLORER_PLAN_VALUES["max_remote_workspaces"]
    # Admin set-plan skips the ally eligibility check.
    ally = client.post(
        "/admin/accounts/somebody@example.com/plan", json={"plan": "ally"}, headers=_admin_key_headers()
    )
    assert ally.status_code == 200
    assert litellm.users_by_id[show["user_id"]]["max_budget"] == ALLY_PLAN_VALUES["monthly_llm_spend_usd"]


def test_admin_set_quota_updates_single_value(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    resp = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_remote_workspaces", "value": 5},
        headers=_admin_key_headers(),
    )
    assert resp.status_code == 200
    show = client.get("/admin/accounts/somebody@example.com", headers=_admin_key_headers()).json()
    assert show["entitlements"]["max_remote_workspaces"] == 5
    # Other values are untouched.
    assert show["entitlements"]["max_buckets"] == EXPLORER_PLAN_VALUES["max_buckets"]
    # An LLM budget bump also pushes to LiteLLM.
    resp = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "monthly_llm_spend_usd", "value": 250.5},
        headers=_admin_key_headers(),
    )
    assert resp.status_code == 200
    assert litellm.users_by_id[show["user_id"]]["max_budget"] == 250.5


def test_admin_set_quota_rejects_bad_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    unknown = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_unicorns", "value": 5},
        headers=_admin_key_headers(),
    )
    assert unknown.status_code == 400
    fractional = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_remote_workspaces", "value": 1.5},
        headers=_admin_key_headers(),
    )
    assert fractional.status_code == 400
    negative = client.post(
        "/admin/accounts/somebody@example.com/quota",
        json={"entitlement": "max_remote_workspaces", "value": -1},
        headers=_admin_key_headers(),
    )
    assert negative.status_code == 400


def test_admin_sweep_endpoint_runs_scoped_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    st_backend = make_fake_supertokens_backend()
    st_backend.install_on_app_module(app_mod, monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")
    account_user_id = st_backend.accounts_by_email["somebody@example.com"].user_id
    _seed_entitlements_row(
        entitlements_store, user_id=account_user_id, user_id_prefix="sbprefix", max_total_bucket_bytes=100
    )
    fake.buckets["sbprefix--data"] = {"name": "sbprefix--data"}
    token = fake.create_bucket_token("sbprefix--data", "readwrite", "mngr-r2:sbprefix--data:default")
    store.add_key(str(token["id"]), account_user_id, "sbprefix--data", "readwrite", "default")
    fake.usage_bytes_by_bucket["sbprefix--data"] = 1000

    resp = client.post("/admin/sweep/r2?email=somebody@example.com", headers=_admin_key_headers())
    assert resp.status_code == 200
    counters = resp.json()["counters"]
    assert counters["keys_downgraded"] == 1
    assert fake.account_tokens[str(token["id"])]["access"] == "read"


def test_admin_sweep_endpoint_rejects_supertokens_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep trigger is operator-key gated; a SuperTokens session must not pass."""
    client, _fake, _store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    resp = client.post("/admin/sweep/r2", headers=_user_headers())
    assert resp.status_code == 401


def test_admin_backup_retention_reap_endpoint_reaps_with_window_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    monkeypatch.setenv("MINDS_ADMIN_KEY", _ADMIN_KEY_TEST_VALUE)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    destroyed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    backend.sync_record_rows.append(
        {
            "user_id": _USER_STUB_USER_ID,
            "host_id": "host-abc123",
            "agent_id": "agent-1",
            "state": "destroyed",
            "destroyed_at": destroyed_at,
        }
    )
    bucket_name = f"{derive_user_id_prefix(_USER_STUB_USER_ID)}--host-abc123"
    fake.create_bucket(bucket_name)
    fake.bucket_objects[bucket_name] = ["obj-1"]

    dry = client.post("/admin/sweep/backup-retention?dry_run=1&window_seconds=0", headers=_admin_key_headers())
    assert dry.status_code == 200
    assert dry.json()["result"]["dry_run"] is True
    assert bucket_name in fake.buckets

    real = client.post("/admin/sweep/backup-retention?window_seconds=0", headers=_admin_key_headers())
    assert real.status_code == 200
    assert real.json()["result"]["records_reaped"] == 1
    assert bucket_name not in fake.buckets
    assert backend.sync_record_rows == []


def test_admin_backup_retention_reap_endpoint_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/admin/sweep/backup-retention")
    assert resp.status_code in (401, 403)


def test_route_get_account_reports_plan_entitlements_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    litellm.users_by_id[_USER_STUB_USER_ID] = {
        "user_id": _USER_STUB_USER_ID,
        "spend": 12.5,
        "max_budget": 1000.0,
        "budget_reset_at": "2026-08-01T00:00:00Z",
    }
    resp = client.get("/account", headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == _USER_STUB_USER_ID
    assert body["email"] == _USER_STUB_EMAIL
    # Stub email is paid-listed + pre-cutoff, so the lazily-created plan is ally.
    assert body["plan_name"] == "ally"
    assert body["entitlements"]["max_remote_workspaces"] == ALLY_PLAN_VALUES["max_remote_workspaces"]
    assert body["usage"]["remote_workspaces"] == 1
    assert body["usage"]["llm_spend_usd_this_period"] == 12.5
    assert body["usage"]["llm_budget_resets_at"] == "2026-08-01T00:00:00Z"
    assert sorted(body["available_plans"]) == ["ally", "explorer", "free"]
    # CLEANUP: drop with the deprecated tunnel compat fields (see accounts.py).
    # v0.3.11 clients require these tunnel-era fields with no defaults.
    assert body["entitlements"]["max_tunnels"] == 0
    assert body["entitlements"]["max_services_per_tunnel"] == 0
    assert body["usage"]["tunnels"] == 0


def test_admin_get_account_reports_suspension_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm, st_backend = _make_account_admin_test_client(monkeypatch)
    st_backend.sign_up(tenant_id="public", email="somebody@example.com", password="password123")

    fresh = client.get("/admin/accounts/somebody@example.com", headers=_admin_key_headers()).json()
    assert fresh["suspended_at"] is None
    assert fresh["suspended_reason"] is None

    entitlements_store.update_entitlements(
        fresh["user_id"], {"suspended_at": "2026-08-22T00:00:00+00:00", "suspended_reason": "abuse"}
    )
    suspended = client.get("/admin/accounts/somebody@example.com", headers=_admin_key_headers()).json()
    assert suspended["suspended_at"] == "2026-08-22T00:00:00+00:00"
    assert suspended["suspended_reason"] == "abuse"
