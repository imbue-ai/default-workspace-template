"""Tunnel, service, service-token, and sharing endpoints (/tunnels/*, /sharing/*)."""

import logging

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.auth as auth_module
import imbue.remote_service_connector.entitlements as entitlements_module
import imbue.remote_service_connector.forwarding as forwarding_module
from imbue.remote_service_connector.auth import AuthResult
from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.auth import authenticate_request
from imbue.remote_service_connector.auth import require_tunnel_access
from imbue.remote_service_connector.auth import require_user_auth
from imbue.remote_service_connector.cloudflare import CloudflareOps
from imbue.remote_service_connector.entitlements import AccountEntitlements
from imbue.remote_service_connector.entitlements import PLAN_EXPLORER
from imbue.remote_service_connector.entitlements import raise_quota_exceeded
from imbue.remote_service_connector.errors import PlanNotFoundError
from imbue.remote_service_connector.forwarding import AuthPolicy
from imbue.remote_service_connector.forwarding import ServiceInfo
from imbue.remote_service_connector.forwarding import owner_email_auth_policy
from imbue.remote_service_connector.forwarding import validate_auth_policy_has_identity
from imbue.remote_service_connector.http_api import handle_endpoint_errors
from imbue.remote_service_connector.naming import TUNNEL_NAME_SEP
from imbue.remote_service_connector.naming import extract_user_id_prefix_from_tunnel_name
from imbue.remote_service_connector.naming import make_tunnel_name

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateTunnelRequest(BaseModel):
    agent_id: str = Field(description="The mngr agent ID for this tunnel")
    default_auth_policy: AuthPolicy | None = Field(
        default=None, description="Optional default auth policy for new services"
    )


class AddServiceRequest(BaseModel):
    service_name: str = Field(description="User-chosen name for the service")
    service_url: str = Field(description="Local service URL (e.g. http://localhost:8080)")


class EnableSharingRequest(BaseModel):
    agent_id: str = Field(description="The mngr agent ID whose tunnel hosts the service")
    service_name: str = Field(description="User-chosen name for the service")
    service_url: str = Field(description="Local service URL (e.g. http://localhost:8080)")
    auth_policy: AuthPolicy = Field(description="Access policy applied to the shared service; must carry identity")


class CreateServiceTokenRequest(BaseModel):
    name: str = Field(description="Human-readable name for the service token")


def count_user_tunnels(ops: CloudflareOps, user_id_prefix: str) -> int:
    """Count the user's tunnels.

    Shared by the tunnel quota check (``POST /tunnels``) and the ``/account``
    usage display so the two can never drift.
    """
    prefix = f"{user_id_prefix}{TUNNEL_NAME_SEP}"
    return len([t for t in ops.list_tunnels(include_prefix=prefix) if t["name"].startswith(prefix)])


def enforce_tunnel_quota_for_new_tunnel(
    ops: CloudflareOps, user_id_prefix: str, tunnel_name: str, entitlements: AccountEntitlements
) -> None:
    """Refuse creating ``tunnel_name`` when it does not exist yet and the account is at ``max_tunnels``.

    Idempotent re-creates of an existing tunnel are always allowed, so the
    count is only checked when the tunnel is absent. Shared by ``POST
    /tunnels`` and ``POST /sharing/enable`` so the two enforcement points
    cannot drift.
    """
    if ops.get_tunnel_by_name(tunnel_name) is not None:
        return
    current = count_user_tunnels(ops, user_id_prefix)
    if current >= entitlements.max_tunnels:
        raise_quota_exceeded("max_tunnels", entitlements.max_tunnels, current, "tunnels")


@router.post("/tunnels")
def create_tunnel(request: Request, body: CreateTunnelRequest) -> dict[str, object]:
    """Create a tunnel (idempotent) and return its info with token.

    Enforces the account's tunnel quota (idempotent re-creates of an existing
    tunnel are always allowed), validates any provided default auth policy,
    and -- when none is provided -- installs an allow-only-the-owner's-email
    default so services added later are never publicly reachable.
    """
    with handle_endpoint_errors():
        ctx = forwarding_module.get_ctx()
        auth = authenticate_request(request, ctx.ops)
        user = require_user_auth(auth)
        entitlements = entitlements_module.resolve_entitlements_for_user(request, user)
        if body.default_auth_policy is not None:
            validate_auth_policy_has_identity(body.default_auth_policy)
        tunnel_name = make_tunnel_name(user.user_id_prefix, body.agent_id)
        enforce_tunnel_quota_for_new_tunnel(ctx.ops, user.user_id_prefix, tunnel_name, entitlements)
        fallback = owner_email_auth_policy(user.email) if user.email else None
        return ctx.create_tunnel(
            user.user_id_prefix,
            body.agent_id,
            default_auth_policy=body.default_auth_policy,
            fallback_auth_policy=fallback,
        ).model_dump()


@router.get("/tunnels")
def list_tunnels(request: Request) -> list[dict[str, object]]:
    """List all tunnels belonging to the authenticated user."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user = require_user_auth(auth)
        return [t.model_dump() for t in forwarding_module.get_ctx().list_tunnels(user.user_id_prefix)]


@router.get("/tunnels/by-agent/{agent_id}")
def get_tunnel_for_agent(request: Request, agent_id: str) -> dict[str, object] | None:
    """Resolve the authenticated user's tunnel for ``agent_id`` (O(1) lookup).

    Uses Cloudflare's server-side name filter plus one config fetch (2
    Cloudflare calls) instead of the O(n) ``GET /tunnels`` path that
    enumerates every tunnel and fetches each one's config. The static
    ``by-agent`` prefix can never collide with a real ``{tunnel_name}``
    (those always contain the ``--`` separator), so there is no ambiguity
    with the other ``/tunnels/*`` routes.

    Returns HTTP 200 with ``null`` when the user has no tunnel for the agent
    yet (rather than 404). This is deliberate: a client hitting a connector
    that predates this endpoint gets FastAPI's generic 404-for-unknown-route,
    so reserving 404 exclusively for "endpoint absent" lets the client tell
    "this connector is too old, fall back to enumerating ``GET /tunnels``"
    apart from "the endpoint works and there is simply no tunnel" (200 null).
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user = require_user_auth(auth)
        tunnel = forwarding_module.get_ctx().get_tunnel_for_agent(user.user_id_prefix, agent_id)
        return tunnel.model_dump() if tunnel is not None else None


@router.delete("/tunnels/{tunnel_name}")
def delete_tunnel(request: Request, tunnel_name: str) -> dict[str, str]:
    """Delete a tunnel and all its associated DNS records, Access Applications, ingress rules, and KV entries.

    Idempotent at the HTTP layer -- a second DELETE on an already-gone
    tunnel returns 200 with ``status: already_deleted`` rather than
    404. Clients retrying after a transient error therefore don't have
    to special-case ``404 Not Found``.
    """
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user = require_user_auth(auth)
        try:
            forwarding_module.get_ctx().delete_tunnel(tunnel_name, user.user_id_prefix)
        except HTTPException as exc:
            if exc.status_code == 404:
                return {"status": "already_deleted"}
            raise
        return {"status": "deleted"}


def _service_quota_and_owner_email(request: Request, auth: AuthResult, tunnel_name: str) -> tuple[int, str | None]:
    """Resolve the services-per-tunnel limit and the owner's email for either auth kind.

    User auth resolves (lazily creating) the caller's entitlements row and
    uses their verified email. Tunnel-token auth only knows the tunnel-name
    prefix: it reads the row by prefix (created earlier, at user-authed tunnel
    creation) and looks the owner's email up from SuperTokens; a missing row
    falls back to the explorer plan's limit with no derivable owner email.
    """
    if isinstance(auth, UserAuth):
        entitlements = entitlements_module.resolve_entitlements_for_user(request, auth)
        return entitlements.max_services_per_tunnel, auth.email
    prefix = extract_user_id_prefix_from_tunnel_name(tunnel_name)
    store = entitlements_module.get_entitlements_store()
    row = store.get_entitlements_by_prefix(prefix)
    if row is not None:
        entitlements = AccountEntitlements(**row)
        return entitlements.max_services_per_tunnel, auth_module.default_email_getter(entitlements.user_id)
    plan = store.get_plan(PLAN_EXPLORER)
    if plan is None:
        raise PlanNotFoundError(PLAN_EXPLORER)
    return int(plan["max_services_per_tunnel"]), None


def enforce_service_quota(existing_services: list[ServiceInfo], service_name: str, limit: int) -> None:
    """Refuse adding ``service_name`` when the tunnel is at ``limit`` services.

    Re-adding an existing service is always allowed. Shared by ``POST
    /tunnels/{tunnel_name}/services`` and ``POST /sharing/enable`` so the two
    enforcement points cannot drift.
    """
    if service_name in {s.service_name for s in existing_services}:
        return
    if len(existing_services) >= limit:
        raise_quota_exceeded("max_services_per_tunnel", limit, len(existing_services), "services on this tunnel")


@router.post("/tunnels/{tunnel_name}/services")
def add_service(request: Request, tunnel_name: str, body: AddServiceRequest) -> dict[str, object]:
    """Add a service to a tunnel. Works with both user and tunnel-token auth.

    Enforces the services-per-tunnel quota (re-adding an existing service is
    always allowed) and guarantees the service comes up behind a Cloudflare
    Access Application -- falling back to an owner-email-only policy when the
    tunnel has no stored default, and refusing outright when no policy can be
    derived at all.
    """
    with handle_endpoint_errors():
        ctx = forwarding_module.get_ctx()
        auth = authenticate_request(request, ctx.ops)
        user_id_prefix = require_tunnel_access(auth, tunnel_name)
        limit, owner_email = _service_quota_and_owner_email(request, auth, tunnel_name)
        enforce_service_quota(ctx.list_services(tunnel_name, user_id_prefix), body.service_name, limit)
        fallback = owner_email_auth_policy(owner_email) if owner_email else None
        return ctx.add_service(
            tunnel_name,
            user_id_prefix,
            body.service_name,
            body.service_url,
            fallback_policy=fallback,
        ).model_dump()


@router.delete("/tunnels/{tunnel_name}/services/{service_name}")
def remove_service(request: Request, tunnel_name: str, service_name: str) -> dict[str, str]:
    """Remove a service from a tunnel. Works with both user and tunnel-token auth."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user_id_prefix = require_tunnel_access(auth, tunnel_name)
        forwarding_module.get_ctx().remove_service(tunnel_name, user_id_prefix, service_name)
        return {"status": "deleted"}


@router.get("/tunnels/{tunnel_name}/services")
def list_services(request: Request, tunnel_name: str) -> list[dict[str, object]]:
    """List services on a tunnel. Works with both user and tunnel-token auth."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user_id_prefix = require_tunnel_access(auth, tunnel_name)
        return [s.model_dump() for s in forwarding_module.get_ctx().list_services(tunnel_name, user_id_prefix)]


@router.get("/tunnels/{tunnel_name}/auth")
def get_tunnel_auth(request: Request, tunnel_name: str) -> dict[str, object]:
    """Get the default auth policy for a tunnel."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        require_user_auth(auth)
        policy = forwarding_module.get_ctx().get_tunnel_auth(tunnel_name)
        if policy is None:
            return {"rules": []}
        return policy.model_dump()


@router.put("/tunnels/{tunnel_name}/auth")
def set_tunnel_auth(request: Request, tunnel_name: str, body: AuthPolicy) -> dict[str, str]:
    """Set the default auth policy for a tunnel. Identity-less policies are rejected."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        require_user_auth(auth)
        validate_auth_policy_has_identity(body)
        forwarding_module.get_ctx().set_tunnel_auth(tunnel_name, body)
        return {"status": "updated"}


@router.get("/tunnels/{tunnel_name}/services/{service_name}/auth")
def get_service_auth(request: Request, tunnel_name: str, service_name: str) -> dict[str, object]:
    """Get the auth policy for a specific service."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user = require_user_auth(auth)
        policy = forwarding_module.get_ctx().get_service_auth(tunnel_name, user.user_id_prefix, service_name)
        if policy is None:
            return {"rules": []}
        return policy.model_dump()


@router.post("/tunnels/{tunnel_name}/service-tokens")
def create_service_token_endpoint(
    request: Request, tunnel_name: str, body: CreateServiceTokenRequest
) -> dict[str, object]:
    """Create a service token for programmatic access to this tunnel's services."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user = require_user_auth(auth)
        token = forwarding_module.get_ctx().create_service_token(tunnel_name, user.user_id_prefix, body.name)
        return token.model_dump()


@router.get("/tunnels/{tunnel_name}/service-tokens")
def list_service_tokens_endpoint(request: Request, tunnel_name: str) -> list[dict[str, object]]:
    """List service tokens. Note: secrets are not returned."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        require_user_auth(auth)
        return [t.model_dump() for t in forwarding_module.get_ctx().list_service_tokens()]


@router.put("/tunnels/{tunnel_name}/services/{service_name}/auth")
def set_service_auth(request: Request, tunnel_name: str, service_name: str, body: AuthPolicy) -> dict[str, str]:
    """Set the auth policy for a specific service. Identity-less policies are rejected."""
    with handle_endpoint_errors():
        auth = authenticate_request(request, forwarding_module.get_ctx().ops)
        user = require_user_auth(auth)
        validate_auth_policy_has_identity(body)
        forwarding_module.get_ctx().set_service_auth(tunnel_name, user.user_id_prefix, service_name, body)
        return {"status": "updated"}


@router.post("/sharing/enable")
def enable_sharing_endpoint(request: Request, body: EnableSharingRequest) -> dict[str, object]:
    """Enable (or update) sharing for one service in a single call.

    Collapses the client's previous create-tunnel + add-service +
    set-service-auth sequence -- three round trips, each paying CLI and
    network overhead -- into one request: ensure the tunnel exists
    (idempotent), add the service with the caller's Access policy applied
    directly to its Access Application (replacing a pre-existing app's
    policies on re-enable), and return the resulting tunnel (with token)
    plus the service info, so the caller needs no follow-up status reads.

    Enforces the same quotas as the individual endpoints: the tunnel count
    when a new tunnel would be created, and services-per-tunnel when a new
    service would be added.
    """
    with handle_endpoint_errors():
        ctx = forwarding_module.get_ctx()
        auth = authenticate_request(request, ctx.ops)
        user = require_user_auth(auth)
        entitlements = entitlements_module.resolve_entitlements_for_user(request, user)
        validate_auth_policy_has_identity(body.auth_policy)
        tunnel_name = make_tunnel_name(user.user_id_prefix, body.agent_id)
        enforce_tunnel_quota_for_new_tunnel(ctx.ops, user.user_id_prefix, tunnel_name, entitlements)
        fallback = owner_email_auth_policy(user.email) if user.email else None
        tunnel_info = ctx.create_tunnel(
            user.user_id_prefix,
            body.agent_id,
            default_auth_policy=None,
            fallback_auth_policy=fallback,
        )
        # ``create_tunnel`` already returned the tunnel's current services
        # (empty for a fresh tunnel), so no extra Cloudflare fetch is needed.
        enforce_service_quota(tunnel_info.services, body.service_name, entitlements.max_services_per_tunnel)
        service = ctx.add_service(
            tunnel_name,
            user.user_id_prefix,
            body.service_name,
            body.service_url,
            fallback_policy=fallback,
            service_policy=body.auth_policy,
        )
        return {"tunnel": tunnel_info.model_dump(), "service": service.model_dump()}
