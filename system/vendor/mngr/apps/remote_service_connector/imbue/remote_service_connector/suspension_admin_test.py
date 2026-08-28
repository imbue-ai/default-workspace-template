from typing import Any
from uuid import UUID

import pytest
from supertokens_python.recipe.emailpassword.interfaces import SignUpOkResult as EPSignUpOkResult

from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.shares import derive_share_user_label
from imbue.remote_service_connector.testing import FakeSuperTokensBackend
from imbue.remote_service_connector.testing import _admin_key_headers
from imbue.remote_service_connector.testing import _make_suspension_admin_test_client
from imbue.remote_service_connector.testing import make_storage_config

_TARGET_EMAIL = "suspect@example.com"
_WS_ID = UUID("00000000-0000-0000-0000-00000000bb01")


def _create_target_account(st_backend: FakeSuperTokensBackend, session_count: int = 1) -> str:
    signup = st_backend.sign_up(tenant_id="public", email=_TARGET_EMAIL, password="pw-834721")
    assert isinstance(signup, EPSignUpOkResult)
    for _ in range(session_count):
        st_backend.sdk_create_browser_session(None, signup.user.id)
    return signup.user.id


def _seed_leased_workspace(backend: Any, user_id: str, host_id: UUID = _WS_ID) -> Any:
    row = backend.add_available_host(host_id=host_id, version="v1", vps_address="10.0.0.5")
    row.status = "leased"
    row.leased_to_user = derive_user_id_prefix(user_id)
    row.leased_at = "2026-01-01T00:00:00+00:00"
    return row


def _seed_llm_key(litellm: Any, user_id: str, key: str = "sk-suspend-1") -> None:
    litellm.keys_by_id[key] = {"key": key, "user_id": user_id, "spend": 0.0}


def _seed_r2_key(fake_cloudflare: Any, key_store: Any, user_id: str, access: str = "readwrite") -> str:
    bucket_name = f"{derive_user_id_prefix(user_id)}--backups"
    token = fake_cloudflare.create_bucket_token(bucket_name, access, f"token-{bucket_name}")
    key_store.add_key(str(token["id"]), user_id, bucket_name, access, None)
    return str(token["id"])


def test_suspend_requires_the_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _store, _litellm, st_backend, _cf, _keys = _make_suspension_admin_test_client(monkeypatch)
    _create_target_account(st_backend)

    resp = client.post(f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"})

    assert resp.status_code == 401


def test_suspend_requires_a_nonempty_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _store, _litellm, st_backend, _cf, _keys = _make_suspension_admin_test_client(monkeypatch)
    _create_target_account(st_backend)

    resp = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "   "}, headers=_admin_key_headers()
    )

    assert resp.status_code == 400


def test_suspend_unknown_email_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _store, _litellm, _st, _cf, _keys = _make_suspension_admin_test_client(monkeypatch)

    resp = client.post(
        "/admin/accounts/nobody@example.com/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )

    assert resp.status_code == 404


def test_suspend_runs_the_full_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, store, litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend, session_count=2)
    backend.storage_config = make_storage_config()
    _seed_leased_workspace(backend, user_id)
    _seed_llm_key(litellm, user_id)
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    user_label = derive_share_user_label(user_id)
    backend.upsert_share("host-" + "a" * 32, user_label, "US-EAST-VA", f"host-{'a' * 32}.{user_label}.va.minds.wtf")

    resp = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "ssh abuse"}, headers=_admin_key_headers()
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # Flag set with the reason recorded.
    row = store.get_entitlements(user_id)
    assert row is not None
    assert row["suspended_at"] is not None
    assert row["suspended_reason"] == "ssh abuse"
    # Every session revoked.
    assert body["steps"]["sessions"]["revoked_count"] == 2
    assert not any(s.user_id == user_id for s in st_backend.sessions_by_access_token.values())
    # The leased workspace is stopping with a supervisor spawned.
    ws_row = backend.find_pool_row(_WS_ID)
    assert ws_row is not None
    assert ws_row.status == "stopping"
    # The spawned supervisor owns the transition_id the fan-out's stop CAS minted.
    assert ws_row.transition_id is not None
    assert backend.spawned_supervisor_tokens == [(str(_WS_ID), ws_row.transition_id)]
    # The LiteLLM key is blocked.
    assert litellm.keys_by_id["sk-suspend-1"]["blocked"] is True
    # The R2 key is read-only with the suspension marker recorded.
    assert fake_cf.account_tokens[token_id]["access"] == "read"
    stored_key = key_store.get_key(token_id)
    assert stored_key is not None
    assert stored_key["suspension_access"] == "read"
    # The share is suspended (relay token rows kept -- unsuspend self-heals).
    assert all(share["state"] == "suspended" for share in backend.share_rows)


def test_suspend_reports_partial_and_converges_on_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, store, _litellm, st_backend, _cf, _keys = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    _seed_leased_workspace(backend, user_id)
    # No storage config: the workspaces step fails, everything else succeeds.
    backend.storage_config = None

    first = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )

    assert first.status_code == 200
    assert first.json()["status"] == "partial"
    assert first.json()["steps"]["workspaces"]["status"] == "error"
    # The flag landed regardless: the front door is closed even on a partial run.
    row = store.get_entitlements(user_id)
    assert row is not None
    assert row["suspended_at"] is not None
    ws_row = backend.find_pool_row(_WS_ID)
    assert ws_row is not None
    assert ws_row.status == "leased"

    backend.storage_config = make_storage_config()
    second = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )

    assert second.json()["status"] == "ok"
    ws_row_after = backend.find_pool_row(_WS_ID)
    assert ws_row_after is not None
    assert ws_row_after.status == "stopping"


def test_suspend_reports_a_still_starting_workspace_as_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace caught mid-start cannot be stopped yet, so the run must not read as converged."""
    client, backend, _store, _litellm, st_backend, _cf, _keys = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    row = _seed_leased_workspace(backend, user_id)
    row.status = "starting"

    resp = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "partial"
    assert body["steps"]["workspaces"]["status"] == "error"
    assert body["steps"]["workspaces"]["still_starting"] == [str(_WS_ID)]
    # The row itself is untouched; a re-run stops it once it reaches leased.
    ws_row = backend.find_pool_row(_WS_ID)
    assert ws_row is not None
    assert ws_row.status == "starting"


def test_suspend_with_block_storage_disables_tokens_and_rerun_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _store, _litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    token_id = _seed_r2_key(fake_cf, key_store, user_id)

    client.post(f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers())
    assert fake_cf.account_tokens[token_id]["access"] == "read"
    assert fake_cf.account_tokens[token_id].get("status") is None

    escalated = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend",
        json={"reason": "abuse", "block_storage": True},
        headers=_admin_key_headers(),
    )

    assert escalated.json()["status"] == "ok"
    assert fake_cf.account_tokens[token_id]["status"] == "disabled"
    stored_key = key_store.get_key(token_id)
    assert stored_key is not None
    assert stored_key["suspension_access"] == "disabled"


def test_unsuspend_restores_keys_shares_and_signin_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, store, litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    _seed_leased_workspace(backend, user_id)
    _seed_llm_key(litellm, user_id)
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    user_label = derive_share_user_label(user_id)
    backend.upsert_share("host-" + "b" * 32, user_label, "US-EAST-VA", f"host-{'b' * 32}.{user_label}.va.minds.wtf")
    client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend",
        json={"reason": "abuse", "block_storage": True},
        headers=_admin_key_headers(),
    )

    resp = client.post(f"/admin/accounts/{_TARGET_EMAIL}/unsuspend", headers=_admin_key_headers())

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    # Flag cleared (reason wiped with it).
    row = store.get_entitlements(user_id)
    assert row is not None
    assert row["suspended_at"] is None
    assert row["suspended_reason"] is None
    # LiteLLM key unblocked; R2 token re-activated at its original scope.
    assert litellm.keys_by_id["sk-suspend-1"]["blocked"] is False
    assert fake_cf.account_tokens[token_id]["status"] == "active"
    assert fake_cf.account_tokens[token_id]["access"] == "readwrite"
    stored_key = key_store.get_key(token_id)
    assert stored_key is not None
    assert stored_key["suspension_access"] is None
    # Shares are active again; workspaces deliberately stay stopped.
    assert all(share["state"] == "active" for share in backend.share_rows)
    ws_row = backend.find_pool_row(_WS_ID)
    assert ws_row is not None
    assert ws_row.status == "stopping"


def test_unsuspend_restores_quota_downgraded_key_to_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key the quota sweep had downgraded comes back read-only, not readwrite."""
    client, backend, _store, _litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    key_store.set_enforced_access(token_id, "read")
    fake_cf.update_bucket_token_access(token_id, "bucket", "read", "token")

    client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend",
        json={"reason": "abuse", "block_storage": True},
        headers=_admin_key_headers(),
    )
    client.post(f"/admin/accounts/{_TARGET_EMAIL}/unsuspend", headers=_admin_key_headers())

    assert fake_cf.account_tokens[token_id]["status"] == "active"
    assert fake_cf.account_tokens[token_id]["access"] == "read"
    stored_key = key_store.get_key(token_id)
    assert stored_key is not None
    assert stored_key["enforced_access"] == "read"
    assert stored_key["suspension_access"] is None


def test_revoke_sessions_endpoint_revokes_all_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, store, _litellm, st_backend, _cf, _keys = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend, session_count=3)

    resp = client.post(f"/admin/accounts/{_TARGET_EMAIL}/revoke-sessions", headers=_admin_key_headers())

    assert resp.status_code == 200
    assert resp.json()["revoked_count"] == 3
    assert not any(s.user_id == user_id for s in st_backend.sessions_by_access_token.values())
    # Revoke alone is not a suspension: no flag is set.
    row = store.get_entitlements(user_id)
    assert row is None or row.get("suspended_at") is None


def test_revoke_sessions_requires_the_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _store, _litellm, st_backend, _cf, _keys = _make_suspension_admin_test_client(monkeypatch)
    _create_target_account(st_backend)

    resp = client.post(f"/admin/accounts/{_TARGET_EMAIL}/revoke-sessions")

    assert resp.status_code == 401


def test_suspend_failed_key_update_leaves_pending_marker_and_rerun_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write-ahead marker: a downgrade whose Cloudflare call fails is recorded as in-flight and retried."""
    client, backend, _store, _litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    fake_cf.fail_next_update_token_access = True

    failed = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )
    assert failed.json()["steps"]["storage_keys"]["failed_count"] == 1
    pending_key = key_store.get_key(token_id)
    assert pending_key is not None
    assert pending_key["suspension_access"] == "pending_read"
    assert fake_cf.account_tokens[token_id]["access"] == "readwrite"

    retried = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )
    assert retried.json()["status"] == "ok"
    assert fake_cf.account_tokens[token_id]["access"] == "read"
    settled_key = key_store.get_key(token_id)
    assert settled_key is not None
    assert settled_key["suspension_access"] == "read"


def test_suspend_downgrades_a_key_with_an_unconfirmed_quota_pending_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quota-'pending' key may still be live-readwrite, so suspend must re-drive the read downgrade.

    The sweep that would normally settle the marker skips suspended
    accounts, so treating 'pending' as already-read here would leave a
    writable key for the whole suspension.
    """
    client, backend, _store, _litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    # Model a crashed quota downgrade whose Cloudflare write never landed:
    # marker pending, live token still readwrite.
    key_store.set_enforced_access(token_id, "pending")

    resp = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )

    assert resp.json()["status"] == "ok"
    assert fake_cf.account_tokens[token_id]["access"] == "read"
    suspended_key = key_store.get_key(token_id)
    assert suspended_key is not None
    assert suspended_key["suspension_access"] == "read"
    # The quota marker is left for the sweep/recheck to settle after unsuspend.
    assert suspended_key["enforced_access"] == "pending"


def test_suspend_leaves_a_confirmed_quota_downgraded_key_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settled enforced_access='read' key is confirmed read-only: nothing to downgrade."""
    client, backend, _store, _litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    fake_cf.account_tokens[token_id]["access"] = "read"
    key_store.set_enforced_access(token_id, "read")

    resp = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )

    assert resp.json()["status"] == "ok"
    assert resp.json()["steps"]["storage_keys"]["downgraded_count"] == 0
    untouched_key = key_store.get_key(token_id)
    assert untouched_key is not None
    assert untouched_key["suspension_access"] is None


def test_suspend_rerun_finishes_an_inflight_disable_without_deescalating(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 'pending_disabled' left by a crashed blocked run is driven to disabled even by a non-blocked re-run."""
    client, backend, _store, _litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    key_store.set_suspension_access(token_id, "pending_disabled")

    resp = client.post(
        f"/admin/accounts/{_TARGET_EMAIL}/suspend", json={"reason": "abuse"}, headers=_admin_key_headers()
    )

    assert resp.json()["status"] == "ok"
    assert fake_cf.account_tokens[token_id]["status"] == "disabled"
    # The prior policy is unknowable behind a 'pending_disabled' marker, so
    # the retried disable records the conservative read scope.
    assert fake_cf.account_tokens[token_id]["access"] == "read"
    settled_key = key_store.get_key(token_id)
    assert settled_key is not None
    assert settled_key["suspension_access"] == "disabled"


def test_unsuspend_reconciles_an_inflight_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsuspending a key stuck at 'pending_disabled' re-activates it whether or not the disable landed."""
    client, backend, _store, _litellm, st_backend, fake_cf, key_store = _make_suspension_admin_test_client(monkeypatch)
    user_id = _create_target_account(st_backend)
    backend.storage_config = make_storage_config()
    token_id = _seed_r2_key(fake_cf, key_store, user_id)
    key_store.set_suspension_access(token_id, "pending_disabled")

    resp = client.post(f"/admin/accounts/{_TARGET_EMAIL}/unsuspend", headers=_admin_key_headers())

    assert resp.status_code == 200
    assert fake_cf.account_tokens[token_id]["status"] == "active"
    assert fake_cf.account_tokens[token_id]["access"] == "readwrite"
    restored_key = key_store.get_key(token_id)
    assert restored_key is not None
    assert restored_key["suspension_access"] is None
