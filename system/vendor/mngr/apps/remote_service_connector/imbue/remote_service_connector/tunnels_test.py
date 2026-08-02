import json
from typing import Any
from uuid import UUID

import pytest

import imbue.remote_service_connector.auth as auth_mod
from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.testing import ALLY_PLAN_VALUES
from imbue.remote_service_connector.testing import _USER_STUB_EMAIL
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _make_pool_test_client
from imbue.remote_service_connector.testing import _make_quota_test_client
from imbue.remote_service_connector.testing import _make_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _user_headers
from imbue.remote_service_connector.testing import make_fake_tunnel_token


def _agent_headers(tunnel_id: str) -> dict[str, str]:
    token = make_fake_tunnel_token(tunnel_id)
    return {"Authorization": f"Bearer {token}"}


def test_route_create_tunnel_as_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["tunnel_name"] == "testuser--agent1"
    assert data["token"] is not None


def test_route_create_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.post("/tunnels", json={"agent_id": "agent2"}, headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_list_tunnels_as_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.get("/tunnels", headers=_user_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_get_tunnel_for_agent_as_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.get("/tunnels/by-agent/agent1", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["tunnel_name"] == "testuser--agent1"


def test_route_get_tunnel_for_agent_returns_null_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # 200 + null (not 404) so a client can tell "no tunnel" apart from
    # "this connector predates the endpoint" (an unknown-route 404).
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels/by-agent/agent1", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json() is None


def test_route_get_tunnel_for_agent_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.get("/tunnels/by-agent/agent1", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_add_service_as_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["service_name"] == "web"


def test_route_add_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 200


def test_route_add_service_agent_wrong_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post("/tunnels", json={"agent_id": "agent2"}, headers=_user_headers())
    resp = client.post(
        "/tunnels/testuser--agent2/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_remove_service_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    resp = client.delete("/tunnels/testuser--agent1/services/web", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 200


def test_route_delete_tunnel_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_agent_headers("tunnel-1"))
    assert resp.status_code == 403


def test_route_set_tunnel_auth_as_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_user_headers(),
    )
    assert resp.status_code == 200


def test_route_get_tunnel_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_user_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_user_headers())
    assert resp.status_code == 200
    assert len(resp.json()["rules"]) == 1


def test_route_set_tunnel_auth_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": []},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_no_auth_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels")
    assert resp.status_code == 401


def test_route_rejects_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """After removing USER_CREDENTIALS, Basic Auth is no longer a supported scheme."""
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels", headers={"Authorization": "Basic dGVzdDp0ZXN0"})
    assert resp.status_code == 401


def test_route_malformed_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.get("/tunnels/foo--bar/services", headers={"Authorization": "Bearer not-valid-base64!!!"})
    assert resp.status_code == 401


def test_route_create_tunnel_too_long_user_id_prefix_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a tunnel whose authenticated user_id_prefix is too long returns 400, not 500."""
    long_name = "a_very_long_user_id_prefix_here_x"
    client = _make_test_client(monkeypatch)
    # Override the stub to return a UserAuth with an overly-long user_id_prefix,
    # simulating a SuperTokens session whose user_id_prefix is longer than the
    # tunnel-naming limit.
    monkeypatch.setattr(
        auth_mod,
        "_authenticate_supertokens",
        lambda _token: UserAuth(user_id_prefix=long_name),
    )
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    assert resp.status_code == 400


def test_route_get_service_auth_reports_owner_email_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service added with no explicit policy carries the owner-email default Access policy."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services/web/auth", headers=_user_headers())
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["include"] == [{"email": {"email": _USER_STUB_EMAIL}}]


def test_route_set_service_auth_as_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /tunnels/.../services/.../auth user path persists the policy."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    resp = client.put(
        "/tunnels/testuser--agent1/services/web/auth",
        json={"rules": [{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}]},
        headers=_user_headers(),
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "updated"}


def test_route_get_tunnel_auth_reports_owner_email_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tunnel created with no explicit policy gets the owner-email default written to KV."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.get("/tunnels/testuser--agent1/auth", headers=_user_headers())
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["include"] == [{"email": {"email": _USER_STUB_EMAIL}}]


def test_route_create_and_list_service_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST/GET /tunnels/.../service-tokens round-trip through ForwardingCtx."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_user_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "my-token"
    assert body["client_secret"] is not None
    resp = client.get("/tunnels/testuser--agent1/service-tokens", headers=_user_headers())
    assert resp.status_code == 200
    listed = resp.json()
    # FakeCloudflareOps.list_service_tokens returns an empty list by design (it
    # doesn't persist created tokens), so the listing is empty -- the test
    # still covers the endpoint + ForwardingCtx.list_service_tokens path.
    assert listed == []


def test_route_service_tokens_agent_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent Bearer auth can't create service tokens (they require a signed-in user)."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.post(
        "/tunnels/testuser--agent1/service-tokens",
        json={"name": "my-token"},
        headers=_agent_headers("tunnel-1"),
    )
    assert resp.status_code == 403


def test_route_list_services_as_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /tunnels/.../services user path lists services."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    resp = client.get("/tunnels/testuser--agent1/services", headers=_user_headers())
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_route_delete_tunnel_as_user_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed-in user can delete a tunnel they own."""
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    resp = client.delete("/tunnels/testuser--agent1", headers=_user_headers())
    assert resp.status_code == 200
    resp = client.get("/tunnels", headers=_user_headers())
    assert resp.json() == []


def test_route_create_tunnel_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloudflare forwarding (`/tunnels/*`) must work even when the user is not paid."""
    client, backend = _make_pool_test_client(monkeypatch)
    # Not-paid: the tunnel route should be unaffected by the paid gate.
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["tunnel_name"] == f"{_USER_STUB_USER_ID_PREFIX}--agent1"


def test_route_list_services_is_not_gated_by_paid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel services routes work for any verified email regardless of paid status."""
    client, backend = _make_pool_test_client(monkeypatch)
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    create_resp = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    assert create_resp.status_code == 200
    list_resp = client.get(f"/tunnels/{_USER_STUB_USER_ID_PREFIX}--agent1/services", headers=_user_headers())
    assert list_resp.status_code == 200


def test_route_create_tunnel_returns_quota_403_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_tunnels=1)
    assert client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers()).status_code == 200
    resp = client.post("/tunnels", json={"agent_id": "agent2"}, headers=_user_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_tunnels"
    # Idempotent re-create of the existing tunnel is always allowed at the cap.
    assert client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers()).status_code == 200


def test_route_add_service_returns_quota_403_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_services_per_tunnel=1)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    first = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    assert first.status_code == 200
    second = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "api", "service_url": "http://localhost:9090"},
        headers=_user_headers(),
    )
    assert second.status_code == 403
    assert second.json()["detail"]["entitlement"] == "max_services_per_tunnel"
    # Re-adding the existing service (an update) is always allowed at the cap.
    re_add = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:9191"},
        headers=_user_headers(),
    )
    assert re_add.status_code == 200


def _enable_sharing_body(service_name: str = "web", email: str = "guest@y.com") -> dict[str, Any]:
    return {
        "agent_id": "agent1",
        "service_name": service_name,
        "service_url": "http://localhost:8080",
        "auth_policy": {"rules": [{"action": "allow", "include": [{"email": {"email": email}}]}]},
    }


def test_route_enable_sharing_creates_tunnel_service_and_policy_in_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    resp = client.post("/sharing/enable", json=_enable_sharing_body(), headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["tunnel"]["tunnel_name"] == "testuser--agent1"
    assert body["tunnel"]["token"] is not None
    assert body["service"]["service_name"] == "web"
    assert body["service"]["hostname"]
    # The requested policy landed on the service's Access Application.
    auth = client.get("/tunnels/testuser--agent1/services/web/auth", headers=_user_headers()).json()
    assert "guest@y.com" in json.dumps(auth)


def test_route_enable_sharing_re_enable_replaces_the_service_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    assert (
        client.post("/sharing/enable", json=_enable_sharing_body(email="a@x.com"), headers=_user_headers()).status_code
        == 200
    )
    assert (
        client.post("/sharing/enable", json=_enable_sharing_body(email="b@y.com"), headers=_user_headers()).status_code
        == 200
    )
    services = client.get("/tunnels/testuser--agent1/services", headers=_user_headers()).json()
    assert len(services) == 1
    auth = json.dumps(client.get("/tunnels/testuser--agent1/services/web/auth", headers=_user_headers()).json())
    assert "b@y.com" in auth
    assert "a@x.com" not in auth


def test_route_enable_sharing_enforces_tunnel_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_tunnels=1)
    client.post("/tunnels", json={"agent_id": "other"}, headers=_user_headers())
    resp = client.post("/sharing/enable", json=_enable_sharing_body(), headers=_user_headers())
    assert resp.status_code == 403
    assert resp.json()["detail"]["entitlement"] == "max_tunnels"


def test_route_enable_sharing_enforces_service_quota_but_allows_re_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_services_per_tunnel=1)
    assert client.post("/sharing/enable", json=_enable_sharing_body("web"), headers=_user_headers()).status_code == 200
    blocked = client.post("/sharing/enable", json=_enable_sharing_body("api"), headers=_user_headers())
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["entitlement"] == "max_services_per_tunnel"
    assert client.post("/sharing/enable", json=_enable_sharing_body("web"), headers=_user_headers()).status_code == 200


def test_route_enable_sharing_rejects_identityless_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    body = _enable_sharing_body()
    body["auth_policy"] = {"rules": []}
    resp = client.post("/sharing/enable", json=body, headers=_user_headers())
    assert resp.status_code == 400


def test_route_enable_sharing_requires_user_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    created = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers()).json()
    resp = client.post("/sharing/enable", json=_enable_sharing_body(), headers=_agent_headers(created["tunnel_id"]))
    assert resp.status_code == 403


def test_route_add_service_agent_auth_respects_owner_quota_by_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent (tunnel-token) auth resolves the owner's quota via the tunnel-name prefix."""
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_services_per_tunnel=1)
    created = client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers()).json()
    agent = _agent_headers(created["tunnel_id"])
    first = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=agent,
    )
    assert first.status_code == 200
    second = client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "api", "service_url": "http://localhost:9090"},
        headers=agent,
    )
    assert second.status_code == 403
    assert second.json()["detail"]["entitlement"] == "max_services_per_tunnel"


def test_route_create_tunnel_rejects_identity_less_default_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    resp = client.post(
        "/tunnels",
        json={"agent_id": "agent1", "default_auth_policy": {"rules": []}},
        headers=_user_headers(),
    )
    assert resp.status_code == 400
    resp = client.post(
        "/tunnels",
        json={
            "agent_id": "agent1",
            "default_auth_policy": {"rules": [{"action": "allow", "include": [{"everyone": {}}]}]},
        },
        headers=_user_headers(),
    )
    assert resp.status_code == 400


def test_route_set_tunnel_auth_rejects_identity_less_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    empty = client.put("/tunnels/testuser--agent1/auth", json={"rules": []}, headers=_user_headers())
    assert empty.status_code == 400
    everyone = client.put(
        "/tunnels/testuser--agent1/auth",
        json={"rules": [{"action": "allow", "include": [{"everyone": {}}]}]},
        headers=_user_headers(),
    )
    assert everyone.status_code == 400


def test_route_set_service_auth_rejects_identity_less_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_test_client(monkeypatch)
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
    client.post(
        "/tunnels/testuser--agent1/services",
        json={"service_name": "web", "service_url": "http://localhost:8080"},
        headers=_user_headers(),
    )
    resp = client.put(
        "/tunnels/testuser--agent1/services/web/auth",
        json={"rules": [{"action": "allow", "include": []}]},
        headers=_user_headers(),
    )
    assert resp.status_code == 400


def test_route_get_account_reports_plan_entitlements_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, entitlements_store, litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=UUID("00000000-0000-0000-0000-000000000042"),
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
    )
    client.post("/tunnels", json={"agent_id": "agent1"}, headers=_user_headers())
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
    assert body["usage"]["tunnels"] == 1
    assert body["usage"]["llm_spend_usd_this_period"] == 12.5
    assert body["usage"]["llm_budget_resets_at"] == "2026-08-01T00:00:00Z"
    assert sorted(body["available_plans"]) == ["ally", "explorer"]
