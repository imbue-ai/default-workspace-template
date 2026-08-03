import base64

import httpx
import pytest

import imbue.remote_service_connector.litellm_client as litellm_client_mod
from imbue.remote_service_connector.sync import _MAX_KEY_BUNDLE_FIELD_BYTES
from imbue.remote_service_connector.testing import ALLY_PLAN_VALUES
from imbue.remote_service_connector.testing import _USER_STUB_EMAIL
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _make_sync_test_client
from imbue.remote_service_connector.testing import _user_headers


def test_route_create_litellm_key_refused_for_zero_budget_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explorer account (monthly LLM budget 0) cannot mint imbue-cloud keys.

    The refusal happens before any LiteLLM HTTP call and carries the
    structured quota detail plus the subscription guidance.
    """
    client, backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    resp = client.post("/keys/create", json={}, headers=_user_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "monthly_llm_spend_usd"
    assert "subscription" in detail["message"]
    assert litellm.calls == []


def test_route_create_litellm_key_upserts_user_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minting a key first pushes the account's monthly budget to LiteLLM as a user budget."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    resp = client.post("/keys/create", json={"key_alias": "my-agent"}, headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["key"].startswith("sk-fake-")
    user = litellm.users_by_id[_USER_STUB_USER_ID]
    assert user["max_budget"] == ALLY_PLAN_VALUES["monthly_llm_spend_usd"]
    assert user["budget_duration"] == "1mo"
    assert litellm.generated_keys[0]["user_id"] == _USER_STUB_USER_ID


def test_route_create_litellm_key_fails_when_budget_push_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A LiteLLM outage during the budget upsert fails the mint (no key is created)."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    litellm.fail_user_writes = True
    resp = client.post("/keys/create", json={}, headers=_user_headers())
    assert resp.status_code == 500
    assert litellm.generated_keys == []


def test_route_list_litellm_keys_works_for_unpaid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing keys needs no quota -- an unpaid (explorer) account gets its (empty) list."""
    client, backend, _entitlements_store, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    resp = client.get("/keys", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_route_get_litellm_key_enforces_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key info is only served to the key's owner."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    created = client.post("/keys/create", json={}, headers=_user_headers()).json()
    owned = client.get(f"/keys/{created['key']}", headers=_user_headers())
    assert owned.status_code == 200
    assert owned.json()["user_id"] == _USER_STUB_USER_ID
    litellm.keys_by_id[created["key"]]["user_id"] = "someone-else"
    foreign = client.get(f"/keys/{created['key']}", headers=_user_headers())
    assert foreign.status_code == 403


def test_route_update_and_delete_litellm_key_work_without_paid_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Budget update + delete only require ownership, not any plan gate."""
    client, _backend, _entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    created = client.post("/keys/create", json={}, headers=_user_headers()).json()
    resp = client.put(f"/keys/{created['key']}/budget", json={"max_budget": 5.0}, headers=_user_headers())
    assert resp.status_code == 200
    assert litellm.keys_by_id[created["key"]]["max_budget"] == 5.0
    resp = client.delete(f"/keys/{created['key']}", headers=_user_headers())
    assert resp.status_code == 200
    assert created["key"] not in litellm.keys_by_id


def test_key_bundle_round_trip_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    assert client.get("/sync/bundle", headers=_user_headers()).status_code == 404

    body = {
        "kdf_salt": base64.b64encode(b"0123456789abcdef").decode("ascii"),
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": base64.b64encode(b"wrapped-dek-bytes").decode("ascii"),
        "key_epoch": 1,
    }
    assert client.put("/sync/bundle", json=body, headers=_user_headers()).status_code == 200

    fetched = client.get("/sync/bundle", headers=_user_headers())
    assert fetched.status_code == 200
    assert fetched.json()["wrapped_dek"] == body["wrapped_dek"]
    assert fetched.json()["kdf_salt"] == body["kdf_salt"]
    assert fetched.json()["key_epoch"] == 1

    assert client.delete("/sync/bundle", headers=_user_headers()).status_code == 200
    assert client.get("/sync/bundle", headers=_user_headers()).status_code == 404


def test_key_bundle_rejects_oversized_wrapped_dek(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    body = {
        "kdf_salt": base64.b64encode(b"0123456789abcdef").decode("ascii"),
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": base64.b64encode(b"x" * (_MAX_KEY_BUNDLE_FIELD_BYTES + 1)).decode("ascii"),
        "key_epoch": 1,
    }
    assert client.put("/sync/bundle", json=body, headers=_user_headers()).status_code == 400


def test_get_litellm_user_spend_reports_zero_when_litellm_unreachable() -> None:
    """A transport-level LiteLLM failure degrades the display-only spend to zero (no 500)."""

    def _raise_transport_error(
        method: str, path: str, json_body: dict[str, object] | None = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert litellm_client_mod.get_litellm_user_spend("user-1", request_fn=_raise_transport_error) == (0.0, None)
