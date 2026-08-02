from typing import Any

import pytest

import imbue.remote_service_connector.errors as errors_mod
import imbue.remote_service_connector.forwarding as forwarding_mod
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import ServiceNotFoundError
from imbue.remote_service_connector.errors import TunnelNotFoundError
from imbue.remote_service_connector.errors import TunnelOwnershipError
from imbue.remote_service_connector.forwarding import AuthPolicy
from imbue.remote_service_connector.forwarding import ForwardingCtx
from imbue.remote_service_connector.naming import make_hostname
from imbue.remote_service_connector.testing import FakeCloudflareOps
from imbue.remote_service_connector.testing import make_fake_forwarding_ctx


def _email_policy(email: str) -> AuthPolicy:
    """Build the allow-only-this-email AuthPolicy used across the tunnel/service tests."""
    return AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": email}}]}])


def test_create_tunnel() -> None:
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    assert info.tunnel_name == "alice--agent1"
    assert info.token == "token-for-tunnel-1"
    assert info.services == []


def test_create_tunnel_with_default_auth() -> None:
    ctx = make_fake_forwarding_ctx()
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    info = ctx.create_tunnel("alice", "agent1", default_auth_policy=policy)
    assert info.tunnel_name == "alice--agent1"
    stored = ctx.get_tunnel_auth("alice--agent1")
    assert stored is not None
    assert len(stored.rules) == 1


def test_create_tunnel_reuses_existing() -> None:
    ctx = make_fake_forwarding_ctx()
    info1 = ctx.create_tunnel("alice", "agent1")
    info2 = ctx.create_tunnel("alice", "agent1")
    assert info1.tunnel_id == info2.tunnel_id


def test_list_tunnels_filters_by_user() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.create_tunnel("alice", "agent2")
    ctx.create_tunnel("bob", "agent3")
    tunnels = ctx.list_tunnels("alice")
    assert len(tunnels) == 2


def test_get_tunnel_for_agent_returns_none_when_absent() -> None:
    ctx = make_fake_forwarding_ctx()
    assert ctx.get_tunnel_for_agent("alice", "agent1") is None


def test_get_tunnel_for_agent_returns_tunnel_with_services() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    tunnel = ctx.get_tunnel_for_agent("alice", "agent1")
    assert tunnel is not None
    assert tunnel.tunnel_name == "alice--agent1"
    assert [s.service_name for s in tunnel.services] == ["web"]


class _CallCountingCloudflareOps(FakeCloudflareOps):
    """FakeCloudflareOps that counts the O(n)-prone tunnel calls.

    Used to assert the ``get_tunnel_for_agent`` fast path never enumerates the
    account (``list_tunnels``) and fetches only the matched tunnel's config.
    """

    def __init__(self) -> None:
        super().__init__()
        self.list_tunnels_calls = 0
        self.get_tunnel_config_calls = 0

    def list_tunnels(self, include_prefix: str = "") -> list[dict[str, Any]]:
        self.list_tunnels_calls += 1
        return super().list_tunnels(include_prefix=include_prefix)

    def get_tunnel_config(self, tunnel_id: str) -> dict[str, Any]:
        self.get_tunnel_config_calls += 1
        return super().get_tunnel_config(tunnel_id)


def test_get_tunnel_for_agent_targets_by_name_not_enumeration() -> None:
    """The O(1) lookup must resolve the exact tunnel without enumerating the
    account (``list_tunnels``) or fetching every tunnel's config.

    Creates many tunnels for the user, then counts the expensive calls: the
    lookup must hit ``get_tunnel_config`` exactly once (for the matched
    tunnel) and never call ``list_tunnels``.
    """
    ops = _CallCountingCloudflareOps()
    ctx = ForwardingCtx(ops=ops, domain="example.com")
    for i in range(10):
        ctx.create_tunnel("alice", f"agent{i}")
    ops.get_tunnel_config_calls = 0
    ops.list_tunnels_calls = 0
    tunnel = ctx.get_tunnel_for_agent("alice", "agent7")
    assert tunnel is not None
    assert tunnel.tunnel_name == "alice--agent7"
    assert ops.get_tunnel_config_calls == 1
    assert ops.list_tunnels_calls == 0


def test_delete_tunnel_cascades() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.delete_tunnel("alice--agent1", "alice")
    assert len(ctx.fake.tunnels) == 0
    assert len(ctx.fake.dns_records) == 0
    assert ctx.fake.kv_get("alice--agent1") is None


def test_delete_tunnel_raises_for_wrong_owner() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(TunnelOwnershipError):
        ctx.delete_tunnel("alice--agent1", "bob")


def test_add_service_creates_dns_and_ingress() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    info = ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert info.hostname == "web--agent1--alice.example.com"
    assert len(ctx.fake.dns_records) == 1


def test_add_service_applies_default_access_policy() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert len(ctx.fake.access_apps) == 1
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert len(ctx.fake.access_policies.get(app_id, [])) == 1


def test_add_service_passes_allowed_idps_to_access_app() -> None:
    """When ForwardingCtx has allowed_idps configured, they are passed to created Access Applications."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123"])
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert ctx.fake.access_apps[app_id]["allowed_idps"] == ["google-idp-uuid-123"]


def test_add_service_no_allowed_idps_when_not_configured() -> None:
    """When allowed_idps is None, it is not included in the Access Application."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert "allowed_idps" not in ctx.fake.access_apps[app_id]


def test_set_service_auth_passes_allowed_idps() -> None:
    """set_service_auth creates Access Applications with allowed_idps when configured."""
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp-uuid-123", "otp-idp-uuid-456"])
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    app_id = list(ctx.fake.access_apps.keys())[0]
    assert ctx.fake.access_apps[app_id]["allowed_idps"] == ["google-idp-uuid-123", "otp-idp-uuid-456"]


def test_add_service_is_idempotent() -> None:
    """Calling ``add_service`` twice for the same hostname should succeed without
    creating a duplicate CNAME or duplicate ingress rule.

    Real Cloudflare returns error 81053 ("DNS record already exists") on the
    second ``create_cname`` call -- ``FakeCloudflareOps`` mirrors that. Before
    this fix, the minds "Update sharing" flow re-ran ``add_service`` on every
    submit and surfaced the connector's 400/81053 error to the user.
    """
    ctx = make_fake_forwarding_ctx(allowed_idps=["google-idp"])
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:9090")
    assert len(ctx.fake.dns_records) == 1
    services = ctx.list_services("alice--agent1", "alice")
    assert len(services) == 1
    assert services[0].service_url == "http://localhost:9090"


def test_add_service_preserves_customized_service_auth_on_re_add() -> None:
    """A second ``add_service`` after the user has set a custom service-level
    auth policy must not reset that policy back to the tunnel default."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    default_policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "owner@x.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", default_policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")

    custom_policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "guest@y.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", custom_policy)

    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    result = ctx.get_service_auth("alice--agent1", "alice", "web")
    assert result is not None
    assert result.rules == custom_policy.rules


def test_add_service_rejects_cname_pointing_elsewhere() -> None:
    """If a CNAME for the hostname exists but points at a different tunnel,
    ``add_service`` must refuse rather than silently leak traffic."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    hostname = make_hostname("web", "agent1", "alice", "example.com")
    ctx.fake.dns_records.append(
        {"id": "stray", "name": hostname, "content": "different-tunnel.cfargotunnel.com", "type": "CNAME"}
    )
    with pytest.raises(CloudflareApiError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")


def test_remove_service_deletes_access_app() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert len(ctx.fake.access_apps) == 1
    ctx.remove_service("alice--agent1", "alice", "web")
    assert len(ctx.fake.access_apps) == 0


def test_remove_service_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(ServiceNotFoundError):
        ctx.remove_service("alice--agent1", "alice", "nonexistent")


def test_tunnel_auth_get_set() -> None:
    ctx = make_fake_forwarding_ctx()
    assert ctx.get_tunnel_auth("alice--agent1") is None
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    result = ctx.get_tunnel_auth("alice--agent1")
    assert result is not None
    assert result.rules == policy.rules


def test_service_auth_get_set() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_service_auth("alice--agent1", "alice", "web", policy)
    result = ctx.get_service_auth("alice--agent1", "alice", "web")
    assert result is not None
    assert len(result.rules) == 1


def test_resolve_tunnel_name_by_id() -> None:
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    name = ctx.resolve_tunnel_name_by_id(info.tunnel_id)
    assert name == "alice--agent1"


def test_resolve_tunnel_name_by_id_raises_for_nonexistent() -> None:
    ctx = make_fake_forwarding_ctx()
    with pytest.raises(TunnelNotFoundError):
        ctx.resolve_tunnel_name_by_id("nonexistent")


def test_ctx_set_tunnel_auth_is_persisted_in_kv() -> None:
    """set_tunnel_auth writes the JSON policy to the KV namespace keyed by tunnel name."""
    ctx = make_fake_forwarding_ctx()
    policy = AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": "a@b.com"}}]}])
    ctx.set_tunnel_auth("alice--agent1", policy)
    stored_raw = ctx.fake.kv_get("alice--agent1")
    assert stored_raw is not None
    assert "a@b.com" in stored_raw


def test_ctx_remove_service_scrubs_ingress_rule() -> None:
    """Removing a service drops its hostname from the tunnel config's ingress."""
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("owner@x.com"))
    ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    ctx.remove_service("alice--agent1", "alice", "web")
    config = ctx.fake.tunnel_configs[info.tunnel_id]
    hostnames = [r.get("hostname") for r in config["config"]["ingress"] if "hostname" in r]
    assert hostnames == []


def test_ctx_create_service_token_and_list() -> None:
    """create_service_token persists to the ops layer and returns a ServiceTokenInfo."""
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    token = ctx.create_service_token("alice--agent1", "alice", "svc-1")
    assert token.name == "svc-1"
    assert token.client_secret is not None
    # FakeCloudflareOps.list_service_tokens returns []; list_service_tokens should
    # reflect that rather than pulling from an internal cache.
    assert ctx.list_service_tokens() == []


def test_validate_auth_policy_accepts_identity_rule_types() -> None:
    policy = AuthPolicy(
        rules=[
            {
                "action": "allow",
                "include": [
                    {"email": {"email": "a@b.com"}},
                    {"email_domain": {"domain": "imbue.com"}},
                    {"login_method": {"id": "idp-1"}},
                    {"group": {"id": "group-1"}},
                ],
            }
        ]
    )
    forwarding_mod.validate_auth_policy_has_identity(policy)


def test_ctx_add_service_rolls_back_on_access_app_failure() -> None:
    """A failed Access Application creation must leave nothing behind (no public exposure)."""
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("o@x.com"))
    ctx.fake.fail_next_create_access_app = True
    with pytest.raises(CloudflareApiError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert ctx.fake.dns_records == []
    assert ctx.fake.access_apps == {}
    ingress = ctx.fake.tunnel_configs[info.tunnel_id]["config"]["ingress"]
    assert [r for r in ingress if "hostname" in r] == []


def test_ctx_add_service_rolls_back_access_app_on_policy_failure() -> None:
    """A policy-attachment failure must delete the just-created Access App (no policy-less app remains)."""
    ctx = make_fake_forwarding_ctx()
    info = ctx.create_tunnel("alice", "agent1")
    ctx.set_tunnel_auth("alice--agent1", _email_policy("o@x.com"))
    ctx.fake.fail_next_create_access_policy = True
    with pytest.raises(CloudflareApiError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert ctx.fake.dns_records == []
    assert ctx.fake.access_apps == {}
    ingress = ctx.fake.tunnel_configs[info.tunnel_id]["config"]["ingress"]
    assert [r for r in ingress if "hostname" in r] == []
    # A retry after the transient failure succeeds and attaches the policy.
    retried = ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    app_ids = [a["id"] for a in ctx.fake.access_apps.values() if a["domain"] == retried.hostname]
    assert len(app_ids) == 1
    assert ctx.fake.access_policies[app_ids[0]] != []


def test_ctx_add_service_without_any_policy_is_refused() -> None:
    ctx = make_fake_forwarding_ctx()
    ctx.create_tunnel("alice", "agent1")
    with pytest.raises(errors_mod.ServicePolicyMissingError):
        ctx.add_service("alice--agent1", "alice", "web", "http://localhost:8080")
    assert ctx.fake.dns_records == []


def test_ctx_create_tunnel_fallback_policy_does_not_clobber_existing_default() -> None:
    """Re-creating a tunnel with a fallback must preserve a user-set default policy."""
    ctx = make_fake_forwarding_ctx()
    user_policy = _email_policy("guest@y.com")
    ctx.create_tunnel("alice", "agent1", default_auth_policy=user_policy)
    fallback = forwarding_mod.owner_email_auth_policy("owner@x.com")
    ctx.create_tunnel("alice", "agent1", fallback_auth_policy=fallback)
    stored = ctx.get_tunnel_auth("alice--agent1")
    assert stored is not None
    assert stored.rules == user_policy.rules
