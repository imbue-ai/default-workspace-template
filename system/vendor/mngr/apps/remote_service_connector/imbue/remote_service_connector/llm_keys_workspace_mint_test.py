"""Tests for the workspace-scoped LiteLLM mint endpoint (the web chrome's mint)."""

import pytest
from starlette.testclient import TestClient

from imbue.remote_service_connector.testing import FakeLiteLLMBackend
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _user_headers

_HOST_ID = "host-" + "a" * 32


def _sync_record_body(host_id: str, state: str = "active", revision: int = 1) -> dict[str, object]:
    return {
        "host_id": host_id,
        "agent_id": "agent-mint-test",
        "display_name": "mint workspace",
        "color": "#aabbcc",
        "provider_kind": "imbue_cloud",
        "hosting_device_id": "device-mint",
        "device_label": "web",
        "state": state,
        "restored_from_host_id": None,
        "encrypted_secrets": None,
        "revision": revision,
    }


def _put_record(client: TestClient, host_id: str, state: str = "active", revision: int = 1) -> None:
    resp = client.put(
        f"/sync/records/{host_id}",
        json=_sync_record_body(host_id, state=state, revision=revision),
        headers=_user_headers(),
    )
    assert resp.status_code == 200


def _workspace_key_entries(litellm: FakeLiteLLMBackend, host_id: str) -> list[dict[str, object]]:
    return [key for key in litellm.keys_by_id.values() if key.get("key_alias") == f"workspace-{host_id}"]


def test_workspace_mint_creates_a_key_with_the_deterministic_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, litellm = _make_pool_quota_test_client(monkeypatch)
    _put_record(client, _HOST_ID)

    resp = client.post("/keys/workspace-mint", json={"host_id": _HOST_ID}, headers=_user_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"].startswith("sk-fake-")
    assert body["base_url"] == "https://fake-litellm.example.com"
    entries = _workspace_key_entries(litellm, _HOST_ID)
    assert len(entries) == 1
    minted = entries[0]
    assert minted["max_budget"] == 100.0
    assert minted["budget_duration"] == "1d"
    assert minted["metadata"] == {"workspace_host_id": _HOST_ID, "source": "web-chrome-mint"}


def test_workspace_mint_rotates_an_existing_key_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, litellm = _make_pool_quota_test_client(monkeypatch)
    _put_record(client, _HOST_ID)

    first = client.post("/keys/workspace-mint", json={"host_id": _HOST_ID}, headers=_user_headers())
    second = client.post("/keys/workspace-mint", json={"host_id": _HOST_ID}, headers=_user_headers())

    assert first.status_code == 200 and second.status_code == 200
    first_key = first.json()["key"]
    second_key = second.json()["key"]
    assert first_key != second_key
    # The first key was deleted by the rotation; exactly one key carries the
    # workspace alias afterwards.
    assert first_key not in litellm.keys_by_id
    entries = _workspace_key_entries(litellm, _HOST_ID)
    assert [key["key"] for key in entries] == [second_key]


def test_workspace_mint_refuses_a_host_the_caller_does_not_own(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, litellm = _make_pool_quota_test_client(monkeypatch)

    resp = client.post("/keys/workspace-mint", json={"host_id": _HOST_ID}, headers=_user_headers())

    assert resp.status_code == 403
    assert "workspace record" in resp.json()["detail"]
    assert litellm.generated_keys == []


def test_workspace_mint_refuses_a_destroyed_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, litellm = _make_pool_quota_test_client(monkeypatch)
    _put_record(client, _HOST_ID, state="destroyed")

    resp = client.post("/keys/workspace-mint", json={"host_id": _HOST_ID}, headers=_user_headers())

    assert resp.status_code == 403
    assert litellm.generated_keys == []


def test_workspace_mint_rejects_a_malformed_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _backend, _entitlements, litellm = _make_pool_quota_test_client(monkeypatch)

    resp = client.post("/keys/workspace-mint", json={"host_id": "not-a-host-id"}, headers=_user_headers())

    assert resp.status_code == 400
    assert litellm.generated_keys == []
