"""Cloudflare forwarding business logic: tunnels, services, DNS, Access policies.

``ForwardingCtx`` orchestrates the Cloudflare primitives behind the tunnel /
service endpoints; ``get_ctx`` is the per-container singleton the endpoints
resolve it through (and the seam tests patch).
"""

import functools
import logging
import os
from typing import Any
from typing import Final

import httpx
from pydantic import BaseModel
from pydantic import Field

from imbue.remote_service_connector.cloudflare import CloudflareOps
from imbue.remote_service_connector.cloudflare import HttpCloudflareOps
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.errors import InvalidAuthPolicyError
from imbue.remote_service_connector.errors import ServiceNotFoundError
from imbue.remote_service_connector.errors import ServicePolicyMissingError
from imbue.remote_service_connector.errors import TunnelNotFoundError
from imbue.remote_service_connector.errors import TunnelOwnershipError
from imbue.remote_service_connector.naming import TUNNEL_NAME_SEP
from imbue.remote_service_connector.naming import extract_agent_id_prefix
from imbue.remote_service_connector.naming import extract_service_name
from imbue.remote_service_connector.naming import make_hostname
from imbue.remote_service_connector.naming import make_tunnel_name

logger = logging.getLogger(__name__)


class AuthPolicy(BaseModel):
    rules: list[dict[str, Any]] = Field(description="Cloudflare Access-style policy rules")


class ServiceInfo(BaseModel):
    service_name: str = Field(description="User-chosen service name")
    hostname: str = Field(description="Public hostname for this service")
    service_url: str = Field(description="Backend service URL")


class TunnelInfo(BaseModel):
    tunnel_name: str = Field(description="Tunnel name")
    tunnel_id: str = Field(description="Cloudflare tunnel UUID")
    token: str | None = Field(default=None, description="Tunnel token for cloudflared (only on create)")
    services: list[ServiceInfo] = Field(default_factory=list, description="Configured services")


class ServiceTokenInfo(BaseModel):
    token_id: str = Field(description="Cloudflare service token ID")
    client_id: str = Field(description="Client ID for CF-Access-Client-Id header")
    client_secret: str | None = Field(default=None, description="Client secret (only returned on creation)")
    name: str = Field(description="Token name")


def non_catchall_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rules if "hostname" in r]


def wrap_ingress(rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {"config": {"ingress": list(rules) + [{"service": "http_status:404"}]}}


# ---------------------------------------------------------------------------
# Auth policy helpers
# ---------------------------------------------------------------------------


def policy_to_cf_rules(policy: AuthPolicy) -> list[dict[str, Any]]:
    """Convert our AuthPolicy format to Cloudflare Access policy create/update format."""
    cf_policies = []
    for rule in policy.rules:
        cf_policies.append(
            {
                "name": "Policy rule",
                "decision": rule.get("action", "allow"),
                "include": rule.get("include", []),
                "precedence": len(cf_policies) + 1,
            }
        )
    return cf_policies


def cf_policies_to_auth_policy(cf_policies: list[dict[str, Any]]) -> AuthPolicy:
    """Convert Cloudflare Access policies back to our AuthPolicy format."""
    rules = []
    for p in cf_policies:
        rules.append(
            {
                "action": p.get("decision", "allow"),
                "include": p.get("include", []),
            }
        )
    return AuthPolicy(rules=rules)


# Cloudflare Access include-rule types that constrain access to specific
# identities. Anything outside this set (``everyone``, ``ip``, ...) would let
# a policy make a service publicly reachable, which we do not allow -- Access
# service tokens are the one sanctioned non-identity path and they are managed
# by the dedicated service-token endpoint, never through AuthPolicy bodies.
_IDENTITY_INCLUDE_KEYS: Final = frozenset({"email", "email_domain", "login_method", "group"})


def validate_auth_policy_has_identity(policy: AuthPolicy) -> None:
    """Reject any auth policy that would leave a service publicly reachable.

    Every policy must carry at least one rule, and every rule's ``include``
    list must be non-empty with only identity-constraining entry types.
    Raises :class:`InvalidAuthPolicyError` otherwise.
    """
    if not policy.rules:
        raise InvalidAuthPolicyError("policy must contain at least one rule")
    for rule in policy.rules:
        include = rule.get("include")
        if not isinstance(include, list) or not include:
            raise InvalidAuthPolicyError("every rule must have a non-empty 'include' list")
        for entry in include:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise InvalidAuthPolicyError(f"malformed include entry: {entry!r}")
            (entry_type,) = entry.keys()
            if entry_type not in _IDENTITY_INCLUDE_KEYS:
                raise InvalidAuthPolicyError(
                    f"include type '{entry_type}' is not an identity constraint "
                    f"(allowed: {sorted(_IDENTITY_INCLUDE_KEYS)})"
                )


def owner_email_auth_policy(email: str) -> AuthPolicy:
    """The fallback Access policy: allow only the tunnel owner's verified email."""
    return AuthPolicy(rules=[{"action": "allow", "include": [{"email": {"email": email}}]}])


# ---------------------------------------------------------------------------
# Forwarding service (business logic)
# ---------------------------------------------------------------------------


class ForwardingCtx:
    """Holds the Cloudflare ops abstraction and domain config. Created once per container."""

    def __init__(self, ops: CloudflareOps, domain: str, allowed_idps: list[str] | None = None) -> None:
        self.ops = ops
        self.domain = domain
        self.allowed_idps = allowed_idps

    def verify_ownership(self, tunnel_name: str, user_id_prefix: str) -> None:
        if not tunnel_name.startswith(f"{user_id_prefix}{TUNNEL_NAME_SEP}"):
            raise TunnelOwnershipError(tunnel_name, user_id_prefix)

    def get_tunnel_or_raise(self, tunnel_name: str) -> dict[str, Any]:
        tunnel = self.ops.get_tunnel_by_name(tunnel_name)
        if tunnel is None:
            raise TunnelNotFoundError(tunnel_name)
        return tunnel

    def resolve_tunnel_name_by_id(self, tunnel_id: str) -> str:
        """Look up tunnel name from tunnel ID."""
        tunnel = self.ops.get_tunnel_by_id(tunnel_id)
        if tunnel is None:
            raise TunnelNotFoundError(tunnel_id)
        return tunnel["name"]

    def create_tunnel(
        self,
        user_id_prefix: str,
        agent_id: str,
        default_auth_policy: AuthPolicy | None = None,
        # Applied as the tunnel's default policy only when no default is stored
        # yet (idempotent re-creates must not clobber a user-set default).
        fallback_auth_policy: AuthPolicy | None = None,
    ) -> TunnelInfo:
        name = make_tunnel_name(user_id_prefix, agent_id)
        existing = self.ops.get_tunnel_by_name(name)
        if existing is not None:
            tid = existing["id"]
            token = self.ops.get_tunnel_token(tid)
            services = self._list_services(tid, name, user_id_prefix)
            # Update the default auth policy if provided (may have been missing
            # from the original creation or may need updating)
            if default_auth_policy is not None:
                self.ops.kv_put(name, default_auth_policy.model_dump_json())
            elif fallback_auth_policy is not None and self.ops.kv_get(name) is None:
                self.ops.kv_put(name, fallback_auth_policy.model_dump_json())
            else:
                # A stored default already exists (or no fallback was given);
                # an idempotent re-create must not clobber it.
                pass
            return TunnelInfo(tunnel_name=name, tunnel_id=tid, token=token, services=services)

        result = self.ops.create_tunnel(name)
        tid = result["id"]
        token = self.ops.get_tunnel_token(tid)
        self.ops.put_tunnel_config(tid, wrap_ingress([]))

        effective_policy = default_auth_policy if default_auth_policy is not None else fallback_auth_policy
        if effective_policy is not None:
            self.ops.kv_put(name, effective_policy.model_dump_json())

        return TunnelInfo(tunnel_name=name, tunnel_id=tid, token=token, services=[])

    def list_tunnels(self, user_id_prefix: str) -> list[TunnelInfo]:
        prefix = f"{user_id_prefix}{TUNNEL_NAME_SEP}"
        tunnels = self.ops.list_tunnels(include_prefix=prefix)
        result: list[TunnelInfo] = []
        for t in tunnels:
            name = t["name"]
            if not name.startswith(prefix):
                continue
            tid = t["id"]
            services = self._list_services(tid, name, user_id_prefix)
            result.append(TunnelInfo(tunnel_name=name, tunnel_id=tid, services=services))
        return result

    def get_tunnel_for_agent(self, user_id_prefix: str, agent_id: str) -> TunnelInfo | None:
        """Resolve the caller's tunnel for a single agent in O(1) Cloudflare calls.

        minds always knows the exact tunnel name it wants
        (``<user_id_prefix>--<agent-prefix>``), so this resolves the tunnel via
        Cloudflare's server-side name filter (:func:`cf_get_tunnel_by_name`)
        plus a single config fetch -- 2 Cloudflare calls regardless of how
        many tunnels the account owns. Contrast with :meth:`list_tunnels`,
        which enumerates every tunnel under the user prefix and fetches each
        one's config (O(n) calls). Returns ``None`` when the user has no
        tunnel for the agent yet.
        """
        name = make_tunnel_name(user_id_prefix, agent_id)
        tunnel = self.ops.get_tunnel_by_name(name)
        if tunnel is None:
            return None
        tid = tunnel["id"]
        services = self._list_services(tid, name, user_id_prefix)
        return TunnelInfo(tunnel_name=name, tunnel_id=tid, services=services)

    def delete_tunnel(self, tunnel_name: str, user_id_prefix: str) -> None:
        self.verify_ownership(tunnel_name, user_id_prefix)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        config = self.ops.get_tunnel_config(tid)
        for rule in non_catchall_rules(config.get("config", {}).get("ingress", [])):
            hostname = rule.get("hostname", "")
            if hostname:
                self._delete_access_app_for_hostname(hostname)
                self._delete_dns_by_name(hostname)
        self.ops.put_tunnel_config(tid, wrap_ingress([]))
        self.ops.delete_tunnel(tid)
        self._kv_delete_safe(tunnel_name)

    def add_service(
        self,
        tunnel_name: str,
        user_id_prefix: str,
        service_name: str,
        service_url: str,
        # The Access policy applied when the tunnel has no stored default --
        # typically allow-only-the-owner's-email. When both this and the KV
        # default are absent the add is refused: a service must never go up
        # without an Access Application.
        fallback_policy: AuthPolicy | None = None,
        # When provided, the authoritative policy for this service's Access
        # Application: it wins over the stored tunnel default, and on a
        # re-add it REPLACES a pre-existing app's policies. The combined
        # enable-sharing path passes this so the caller's requested ACL
        # always lands in the same call that brings the service up.
        service_policy: AuthPolicy | None = None,
    ) -> ServiceInfo:
        self.verify_ownership(tunnel_name, user_id_prefix)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        agent_id = extract_agent_id_prefix(tunnel_name, user_id_prefix)
        hostname = make_hostname(service_name, agent_id, user_id_prefix, self.domain)

        # Resolve the Access policy up front and create the Access Application
        # BEFORE any exposure exists (DNS/ingress). A failure here aborts the
        # add outright, so a failed Access call can never leave a service
        # publicly reachable.
        stored_default = self.ops.kv_get(tunnel_name)
        if service_policy is not None:
            policy: AuthPolicy | None = service_policy
        elif stored_default is not None:
            policy = AuthPolicy.model_validate_json(stored_default)
        else:
            policy = fallback_policy
        if policy is None:
            raise ServicePolicyMissingError(tunnel_name)
        created_access_app_id: str | None = None
        is_dns_created_here = False
        try:
            existing_access_app = self.ops.get_access_app_by_domain(hostname)
            if existing_access_app is None:
                access_app = self.ops.create_access_app(hostname, f"cf-fwd-{hostname}", allowed_idps=self.allowed_idps)
                created_access_app_id = access_app["id"]
                for cf_policy in policy_to_cf_rules(policy):
                    self.ops.create_access_policy(access_app["id"], cf_policy)
            elif service_policy is not None:
                # An explicit service policy replaces whatever the pre-existing
                # app carried, so a re-enable always ends at the requested ACL.
                for existing_policy in self.ops.list_access_policies(existing_access_app["id"]):
                    self.ops.delete_access_policy(existing_access_app["id"], existing_policy["id"])
                for cf_policy in policy_to_cf_rules(service_policy):
                    self.ops.create_access_policy(existing_access_app["id"], cf_policy)
            else:
                # A pre-existing app means the service was configured before (with a
                # possibly customized policy) -- leave it untouched on re-add.
                pass

            cname_target = f"{tid}.cfargotunnel.com"
            existing_dns = self.ops.list_dns_records(name=hostname)
            if not existing_dns:
                self.ops.create_cname(hostname, cname_target)
                is_dns_created_here = True
            elif existing_dns[0].get("content") != cname_target:
                raise CloudflareApiError(
                    status_code=409,
                    errors=[
                        {
                            "message": (
                                f"DNS record for {hostname} already exists pointing to "
                                f"{existing_dns[0].get('content')!r}, not {cname_target!r}"
                            )
                        }
                    ],
                )
            else:
                # CNAME already points at this tunnel; idempotent re-add.
                pass
            config = self.ops.get_tunnel_config(tid)
            rules = [
                r
                for r in non_catchall_rules(config.get("config", {}).get("ingress", []))
                if r.get("hostname") != hostname
            ]
            rules.append(
                {
                    "hostname": hostname,
                    "service": service_url,
                    "originRequest": {"noTLSVerify": True},
                }
            )
            self.ops.put_tunnel_config(tid, wrap_ingress(rules))
        except (CloudflareApiError, httpx.HTTPError):
            # Roll back only what this call created (never a pre-existing DNS
            # record or Access App) so a half-added service leaves nothing
            # behind -- in particular nothing publicly reachable.
            if created_access_app_id is not None:
                self._delete_access_app_for_hostname(hostname)
            if is_dns_created_here:
                self._delete_dns_by_name(hostname)
            raise

        return ServiceInfo(service_name=service_name, hostname=hostname, service_url=service_url)

    def remove_service(self, tunnel_name: str, user_id_prefix: str, service_name: str) -> None:
        self.verify_ownership(tunnel_name, user_id_prefix)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        tid = tunnel["id"]
        agent_id = extract_agent_id_prefix(tunnel_name, user_id_prefix)
        hostname = make_hostname(service_name, agent_id, user_id_prefix, self.domain)
        config = self.ops.get_tunnel_config(tid)
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        new_rules = [r for r in rules if r.get("hostname") != hostname]
        if len(new_rules) == len(rules):
            raise ServiceNotFoundError(service_name, tunnel_name)
        self.ops.put_tunnel_config(tid, wrap_ingress(new_rules))
        self._delete_access_app_for_hostname(hostname)
        self._delete_dns_by_name(hostname)

    def get_tunnel_auth(self, tunnel_name: str) -> AuthPolicy | None:
        """Get the default auth policy for a tunnel from KV."""
        raw = self.ops.kv_get(tunnel_name)
        if raw is None:
            return None
        return AuthPolicy.model_validate_json(raw)

    def set_tunnel_auth(self, tunnel_name: str, policy: AuthPolicy) -> None:
        """Set the default auth policy for a tunnel in KV."""
        self.ops.kv_put(tunnel_name, policy.model_dump_json())

    def get_service_auth(self, tunnel_name: str, user_id_prefix: str, service_name: str) -> AuthPolicy | None:
        """Get the auth policy for a specific service from its Access Application."""
        agent_id = extract_agent_id_prefix(tunnel_name, user_id_prefix)
        hostname = make_hostname(service_name, agent_id, user_id_prefix, self.domain)
        access_app = self.ops.get_access_app_by_domain(hostname)
        if access_app is None:
            return None
        policies = self.ops.list_access_policies(access_app["id"])
        return cf_policies_to_auth_policy(policies)

    def set_service_auth(self, tunnel_name: str, user_id_prefix: str, service_name: str, policy: AuthPolicy) -> None:
        """Set the auth policy for a specific service on its Access Application."""
        agent_id = extract_agent_id_prefix(tunnel_name, user_id_prefix)
        hostname = make_hostname(service_name, agent_id, user_id_prefix, self.domain)
        access_app = self.ops.get_access_app_by_domain(hostname)
        if access_app is None:
            access_app = self.ops.create_access_app(hostname, f"cf-fwd-{service_name}", allowed_idps=self.allowed_idps)

        existing_policies = self.ops.list_access_policies(access_app["id"])
        for ep in existing_policies:
            self.ops.delete_access_policy(access_app["id"], ep["id"])

        for cf_policy in policy_to_cf_rules(policy):
            self.ops.create_access_policy(access_app["id"], cf_policy)

    def list_services(self, tunnel_name: str, user_id_prefix: str) -> list[ServiceInfo]:
        """List all services on a tunnel."""
        self.verify_ownership(tunnel_name, user_id_prefix)
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        return self._list_services(tunnel["id"], tunnel_name, user_id_prefix)

    def _list_services(self, tunnel_id: str, tunnel_name: str, user_id_prefix: str) -> list[ServiceInfo]:
        agent_id = extract_agent_id_prefix(tunnel_name, user_id_prefix)
        config = self.ops.get_tunnel_config(tunnel_id)
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        services: list[ServiceInfo] = []
        for rule in rules:
            hostname = rule.get("hostname", "")
            svc_url = rule.get("service", "")
            svc_name = extract_service_name(hostname, agent_id, user_id_prefix, self.domain)
            if svc_name is not None:
                services.append(ServiceInfo(service_name=svc_name, hostname=hostname, service_url=svc_url))
        return services

    def _delete_dns_by_name(self, hostname: str) -> None:
        records = self.ops.list_dns_records(name=hostname)
        for record in records:
            self.ops.delete_dns_record(record["id"])

    def _delete_access_app_for_hostname(self, hostname: str) -> None:
        try:
            access_app = self.ops.get_access_app_by_domain(hostname)
            if access_app is not None:
                self.ops.delete_access_app(access_app["id"])
        except (CloudflareApiError, httpx.HTTPError) as exc:
            logger.warning("Failed to delete Access Application for %s: %s", hostname, exc)

    def _kv_delete_safe(self, key: str) -> None:
        try:
            self.ops.kv_delete(key)
        except (CloudflareApiError, httpx.HTTPError) as exc:
            logger.warning("Failed to delete KV entry for %s: %s", key, exc)

    def create_service_token(self, tunnel_name: str, user_id_prefix: str, name: str) -> ServiceTokenInfo:
        """Create a Cloudflare Access service token and add it to all existing services on the tunnel.

        The service token can be used for programmatic access via
        CF-Access-Client-Id and CF-Access-Client-Secret headers.
        """
        self.verify_ownership(tunnel_name, user_id_prefix)
        result = self.ops.create_service_token(name)
        token_id = result["id"]
        client_id = result["client_id"]
        client_secret = result["client_secret"]

        # Add a non_identity policy for this service token to all existing services
        tunnel = self.get_tunnel_or_raise(tunnel_name)
        config = self.ops.get_tunnel_config(tunnel["id"])
        rules = non_catchall_rules(config.get("config", {}).get("ingress", []))
        for rule in rules:
            hostname = rule.get("hostname", "")
            try:
                access_app = self.ops.get_access_app_by_domain(hostname)
                if access_app is not None:
                    self.ops.create_access_policy(
                        access_app["id"],
                        {
                            "name": f"Service token: {name}",
                            "decision": "non_identity",
                            "include": [{"service_token": {"token_id": token_id}}],
                            "precedence": 10,
                        },
                    )
            except (CloudflareApiError, httpx.HTTPError) as exc:
                logger.warning("Failed to add service token policy for %s: %s", hostname, exc)

        return ServiceTokenInfo(
            token_id=token_id,
            client_id=client_id,
            client_secret=client_secret,
            name=name,
        )

    def list_service_tokens(self) -> list[ServiceTokenInfo]:
        """List all service tokens in the account."""
        tokens = self.ops.list_service_tokens()
        return [
            ServiceTokenInfo(
                token_id=t["id"],
                client_id=t["client_id"],
                client_secret=None,
                name=t["name"],
            )
            for t in tokens
        ]


@functools.cache
def get_ctx() -> ForwardingCtx:
    ops = HttpCloudflareOps(
        api_token=os.environ["CLOUDFLARE_API_TOKEN"],
        account_id=os.environ["CLOUDFLARE_ACCOUNT_ID"],
        zone_id=os.environ["CLOUDFLARE_ZONE_ID"],
    )
    raw_idps = os.environ.get("CLOUDFLARE_ALLOWED_IDPS", "")
    allowed_idps = [s.strip() for s in raw_idps.split(",") if s.strip()] or None
    return ForwardingCtx(ops=ops, domain=os.environ["CLOUDFLARE_DOMAIN"], allowed_idps=allowed_idps)
