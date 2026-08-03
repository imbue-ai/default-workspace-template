"""Self-hosted sharing: share records, relay tokens, and frps plugin auth (/shares/*, /frps/auth).

Replaces the Cloudflare tunnel/Access sharing model (blueprint/sharing-redesign).
A shared workspace lives at
``<service>.<host-id>.<user-label>.<region>.<content-domain>``: the bare
``<host-id>.<user-label>.<region>.<content-domain>`` is the workspace shell,
and ``<user-label>.<region>.<content-domain>`` is the registrable-site
boundary (the Public-Suffix-List entry is ``<region>.<content-domain>``).
The workspace's frpc registers the bare domain plus its wildcard on a relay,
so any service (at any sub-origin depth) routes without per-service DNS or
per-service authorization at the relay. Ids are full and untruncated: host
ids are ``host-<32hex>`` and the user label is the SuperTokens user id with
hyphens stripped (the same normalization ``derive_user_id_prefix`` applies,
without the truncation). Both are opaque and non-secret (they appear in
Certificate Transparency logs).
"""

import functools
import hashlib
import hmac
import logging
import os
import re
import secrets
from typing import Any
from typing import Protocol

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field

import imbue.remote_service_connector.auth as auth_module
from imbue.remote_service_connector import db
from imbue.remote_service_connector.errors import InvalidShareCoordinateError
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.errors import ShareNotFoundError
from imbue.remote_service_connector.errors import ShareQuotaExceededError
from imbue.remote_service_connector.http_api import handle_endpoint_errors

logger = logging.getLogger(__name__)

router = APIRouter()

_SHARE_HOST_ID_RE = re.compile(r"^host-[a-f0-9]{32}$")
_SHARE_USER_LABEL_RE = re.compile(r"^[a-f0-9]{32}$")
_SHARE_DNS_LABEL_RE = re.compile(r"^(?=.{1,63}$)[a-z0-9]+(?:-[a-z0-9]+)*$")

# Per-user quota: how many workspaces one user may have shared at once. Services
# are free (one relay tunnel carries all of a workspace's services), so there is
# no per-service cap.
DEFAULT_MAX_SHARED_WORKSPACES_PER_USER = 50

_RELAY_TOKEN_BYTES = 32

_FRPS_LOGIN_OP = "Login"
_FRPS_NEW_PROXY_OP = "NewProxy"

# frpc carries its relay token in the client metadata map under this key
# (``metadatas.relay_token`` in frpc.toml). Login ops receive the map as
# ``content.metas``; NewProxy ops receive it nested under ``content.user.metas``.
_FRPS_RELAY_TOKEN_META_KEY = "relay_token"

# OVH datacenter code prefix -> region code. us1 is west (Hillsboro), us2 is
# east (Vint Hill). A datacenter that maps to a region with no configured relay
# endpoint (and any unknown datacenter) falls back to SHARE_DEFAULT_REGION.
_SHARE_DATACENTER_REGION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("US-WEST", "us1"),
    ("US-EAST", "us2"),
)


class ShareCoordinate(BaseModel):
    """The hostname coordinates of one shared workspace."""

    host_id: str
    user_label: str
    region: str
    content_domain: str

    @property
    def workspace_domain(self) -> str:
        """The bare workspace origin / registrable base, e.g. ``host-<hex>.<user-label>.<region>.<domain>``."""
        return f"{self.host_id}.{self.user_label}.{self.region}.{self.content_domain}"

    @property
    def vhost_wildcard(self) -> str:
        """The wildcard the relay's frps registers for this workspace's services."""
        return f"*.{self.workspace_domain}"

    @property
    def registrable_site(self) -> str:
        """The per-user registrable site (the Public-Suffix-List-backed isolation boundary)."""
        return f"{self.user_label}.{self.region}.{self.content_domain}"


def derive_share_user_label(user_id: str) -> str:
    """Normalize a SuperTokens user id (a UUID) into its hostname label: hyphens stripped, 32 hex.

    UUIDs are never hyphenated in hostnames or ids anywhere else in the system,
    so the label form matches ``derive_user_id_prefix``'s normalization (without
    the truncation). Raises for anything that is not a UUID-shaped id.
    """
    label = user_id.replace("-", "").lower()
    if _SHARE_USER_LABEL_RE.match(label) is None:
        raise InvalidShareCoordinateError(f"user id must be a UUID (32 hex after stripping hyphens), got {user_id!r}")
    return label


def make_share_coordinate(host_id: str, user_label: str, region: str, content_domain: str) -> ShareCoordinate:
    """Build a validated :class:`ShareCoordinate`.

    Every component must be a legal hostname label (or label run) so the
    resulting workspace domain is a valid, cert-issuable hostname.
    """
    if _SHARE_HOST_ID_RE.match(host_id) is None:
        raise InvalidShareCoordinateError(f"host id must be 'host-<32hex>', got {host_id!r}")
    if _SHARE_USER_LABEL_RE.match(user_label) is None:
        raise InvalidShareCoordinateError(f"user label must be 32 hex characters, got {user_label!r}")
    if _SHARE_DNS_LABEL_RE.match(region) is None:
        raise InvalidShareCoordinateError(f"region must be a DNS label, got {region!r}")
    domain_labels = content_domain.split(".")
    if not all(_SHARE_DNS_LABEL_RE.match(label) is not None for label in domain_labels):
        raise InvalidShareCoordinateError(
            f"content domain must be dot-joined lowercase DNS labels, got {content_domain!r}"
        )
    return ShareCoordinate(host_id=host_id, user_label=user_label, region=region, content_domain=content_domain)


def generate_relay_token() -> str:
    """Mint a fresh opaque relay token (URL-safe, returned to the workspace once)."""
    return secrets.token_urlsafe(_RELAY_TOKEN_BYTES)


def hash_relay_token(token: str) -> str:
    """Hash a relay token for storage / lookup (the plaintext is never stored)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_share_quota(current_active_share_count: int, max_shared_workspaces_per_user: int) -> None:
    """Raise :class:`ShareQuotaExceededError` if one more active share would exceed the cap."""
    if current_active_share_count >= max_shared_workspaces_per_user:
        raise ShareQuotaExceededError(current=current_active_share_count, limit=max_shared_workspaces_per_user)


def require_share_env(name: str) -> str:
    """Read a required sharing env var, raising the 503-mapped config error when unset."""
    value = os.environ.get(name, "")
    if not value:
        raise MissingShareConfigError(name)
    return value


def share_content_domain() -> str:
    """The content domain apex workspace hostnames live under (e.g. ``imbueminds.com``)."""
    return require_share_env("SHARE_CONTENT_DOMAIN")


def parse_relay_endpoint_map(raw: str) -> dict[str, str]:
    """Parse ``SHARE_RELAY_ENDPOINTS`` (``us1=relay-us1.example.com:7000,us2=...``) into region -> endpoint."""
    endpoint_by_region: dict[str, str] = {}
    for entry in raw.split(","):
        stripped_entry = entry.strip()
        if not stripped_entry:
            continue
        region, separator, endpoint = stripped_entry.partition("=")
        if not separator or not region.strip() or not endpoint.strip():
            raise MissingShareConfigError(f"SHARE_RELAY_ENDPOINTS (malformed entry {stripped_entry!r})")
        endpoint_by_region[region.strip()] = endpoint.strip()
    if not endpoint_by_region:
        raise MissingShareConfigError("SHARE_RELAY_ENDPOINTS")
    return endpoint_by_region


def share_relay_endpoint_map() -> dict[str, str]:
    """Region code -> relay tunnel-control endpoint (``host:port`` the workspace's frpc dials)."""
    return parse_relay_endpoint_map(require_share_env("SHARE_RELAY_ENDPOINTS"))


def resolve_share_region(datacenter: str | None) -> str:
    """Pick the region code for a share: the host's datacenter's region when a relay exists there, else the default.

    ``datacenter`` is the pool host's OVH DC code (e.g. ``US-EAST-VA``), or None
    for hosts the connector has no record of (local workspaces). Dev tiers
    configure a single region (the dev env's suffix), so every share lands there
    regardless of datacenter.
    """
    endpoint_by_region = share_relay_endpoint_map()
    default_region = require_share_env("SHARE_DEFAULT_REGION")
    if datacenter:
        datacenter_upper = datacenter.upper()
        for prefix, region in _SHARE_DATACENTER_REGION_PREFIXES:
            if datacenter_upper.startswith(prefix) and region in endpoint_by_region:
                return region
    if default_region not in endpoint_by_region:
        raise MissingShareConfigError(f"SHARE_RELAY_ENDPOINTS (missing default region {default_region!r})")
    return default_region


class FrpsAuthDecision(BaseModel):
    """The reply the connector returns to the frps server plugin.

    ``reject`` rejects the operation with ``reject_reason``; otherwise the
    operation proceeds unchanged (``unchange = True``). frps calls this only for
    ``Login`` and ``NewProxy`` -- never for visitor connections -- so a shared
    workspace's actual traffic never reaches the connector.
    """

    reject: bool
    reject_reason: str = ""
    unchange: bool = True


def _frps_allow() -> FrpsAuthDecision:
    return FrpsAuthDecision(reject=False, unchange=True)


def _frps_reject(reason: str) -> FrpsAuthDecision:
    return FrpsAuthDecision(reject=True, reject_reason=reason, unchange=True)


def decide_frps_new_proxy(
    workspace_domain: str, claimed_custom_domains: list[str], claimed_subdomain: str = ""
) -> FrpsAuthDecision:
    """Authorize an frps ``NewProxy``: every claimed customDomain must be a
    single DNS label directly under this workspace's own domain, nothing else.

    Shared workspaces now claim explicit per-service labels
    (``<label>.<workspace_domain>``) plus a dedicated auth label instead of the
    wildcard, so the relay only routes SNIs the workspace was authorized to
    claim. This is what stops workspace X's frpc from registering workspace Y's
    hostname: the token resolves to X's domain, and any claim that is not
    exactly one label under X's own domain is rejected -- including the bare
    domain and the wildcard, neither of which should route (the bare domain is
    the CT-visible cert name; a wildcard would defeat the point of explicit
    claims). A ``subdomain`` claim is rejected outright: the relay's frps never
    enables subdomain routing (no ``subDomainHost``), and rejecting it here
    keeps that guarantee independent of the relay's rendered config.
    """
    if claimed_subdomain:
        return _frps_reject(f"subdomain claims are not supported (got {claimed_subdomain!r})")
    if not claimed_custom_domains:
        return _frps_reject("NewProxy claimed no custom domains")
    domain = workspace_domain.lower()
    suffix = "." + domain
    for claimed in claimed_custom_domains:
        normalized = claimed.strip().lower()
        if not normalized.endswith(suffix):
            return _frps_reject(f"custom domain {claimed!r} is not under this workspace token's domain")
        label = normalized[: -len(suffix)]
        # Exactly one non-empty label, no wildcard: a single service/auth label.
        if not label or "." in label or "*" in label:
            return _frps_reject(f"custom domain {claimed!r} must be a single label under this workspace's domain")
    return _frps_allow()


def _extract_frps_relay_token(op: str, content: dict[str, Any]) -> str | None:
    """Pull the relay token out of an frps plugin op's metadata map.

    Login ops carry the frpc client metadata at ``content.metas``; NewProxy (and
    other proxy-scoped) ops nest it under ``content.user.metas``. These shapes
    are verified against frp 0.70.1 (the pinned relay release): a Login body is
    ``{op, content: {metas: {relay_token}, ...}}`` and a NewProxy body is
    ``{op, content: {user: {metas: {relay_token}}, custom_domains: [...], ...}}``.
    """
    if op == _FRPS_LOGIN_OP:
        metas = content.get("metas")
    else:
        user = content.get("user")
        metas = user.get("metas") if isinstance(user, dict) else None
    if not isinstance(metas, dict):
        return None
    token = metas.get(_FRPS_RELAY_TOKEN_META_KEY)
    if isinstance(token, str) and token:
        return token
    return None


def _extract_frps_custom_domains(content: dict[str, Any]) -> list[str]:
    domains = content.get("custom_domains")
    if not isinstance(domains, list):
        return []
    return [domain for domain in domains if isinstance(domain, str)]


def _extract_frps_subdomain(content: dict[str, Any]) -> str:
    subdomain = content.get("subdomain")
    return subdomain if isinstance(subdomain, str) else ""


# Columns every share SELECT returns, so row-to-dict projection stays in one place.
_SHARE_COLUMNS = "host_id, user_id, region, workspace_domain, state, created_at, updated_at, last_tunnel_login_at"
_SHARE_COLUMN_NAMES = tuple(name.strip() for name in _SHARE_COLUMNS.split(","))


def _share_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    """Project a ``_SHARE_COLUMNS`` row into a dict, stringifying timestamps."""
    share: dict[str, Any] = dict(zip(_SHARE_COLUMN_NAMES, row, strict=True))
    for timestamp_column in ("created_at", "updated_at", "last_tunnel_login_at"):
        value = share.get(timestamp_column)
        share[timestamp_column] = str(value) if value is not None else None
    return share


class ShareStore(Protocol):
    """Abstraction over the shares / relay_tokens / issued_certs tables so endpoints are unit-testable."""

    def get_share(self, host_id: str, user_label: str) -> dict[str, Any] | None: ...
    def list_shares(self, user_label: str) -> list[dict[str, Any]]: ...
    def activate_share_and_rotate_token(
        self, coordinate: ShareCoordinate, max_active_shares: int, token_hash: str
    ) -> None: ...
    def deactivate_share(self, host_id: str, user_label: str) -> None: ...
    def delete_relay_tokens(self, host_id: str, user_label: str) -> None: ...
    def find_share_by_token_hash(self, token_hash: str) -> dict[str, Any] | None: ...
    def find_active_share_by_workspace_domain(self, workspace_domain: str) -> dict[str, Any] | None: ...
    def record_tunnel_login(self, host_id: str, user_label: str) -> None: ...
    def get_pool_host_datacenter(self, host_id: str) -> str | None: ...
    def get_latest_cert_not_after(self, workspace_domain: str) -> str | None: ...
    def count_certs_issued_in_last_day(self, host_id: str, user_label: str) -> int: ...
    def record_issued_cert(
        self,
        workspace_domain: str,
        host_id: str,
        user_label: str,
        ca_name: str,
        cert_chain_pem: str,
        sans_json: str,
        not_after: str,
    ) -> None: ...


class PostgresShareStore:
    """ShareStore backed by the connector's existing Neon DB."""

    def get_share(self, host_id: str, user_label: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SHARE_COLUMNS} FROM shares WHERE host_id = %s AND user_id = %s",
                    (host_id, user_label),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return _share_row_to_dict(row) if row is not None else None

    def list_shares(self, user_label: str) -> list[dict[str, Any]]:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SHARE_COLUMNS} FROM shares WHERE user_id = %s ORDER BY created_at",
                    (user_label,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_share_row_to_dict(row) for row in rows]

    def activate_share_and_rotate_token(
        self, coordinate: ShareCoordinate, max_active_shares: int, token_hash: str
    ) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize per-user activation so concurrent creates cannot
                    # all pass the quota count (same pattern as lease_host).
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"share-quota:{coordinate.user_label}",),
                    )
                    cur.execute(
                        "SELECT COUNT(*) FROM shares WHERE user_id = %s AND state = 'active' AND host_id <> %s",
                        (coordinate.user_label, coordinate.host_id),
                    )
                    row = cur.fetchone()
                    check_share_quota(int(row[0]) if row is not None else 0, max_active_shares)
                    cur.execute(
                        "INSERT INTO shares (host_id, user_id, region, workspace_domain, state) "
                        "VALUES (%s, %s, %s, %s, 'active') "
                        "ON CONFLICT (host_id, user_id) DO UPDATE SET "
                        "region = EXCLUDED.region, workspace_domain = EXCLUDED.workspace_domain, "
                        "state = 'active', updated_at = NOW()",
                        (coordinate.host_id, coordinate.user_label, coordinate.region, coordinate.workspace_domain),
                    )
                    # The token swap rides the SAME transaction (and the same
                    # advisory lock): a separate transaction would let two
                    # concurrent creates interleave their DELETE+INSERT pairs
                    # (leaving two valid tokens for one share) and a crash
                    # between the two would leave an active share whose relay
                    # token was never written.
                    cur.execute(
                        "DELETE FROM relay_tokens WHERE host_id = %s AND user_id = %s",
                        (coordinate.host_id, coordinate.user_label),
                    )
                    cur.execute(
                        "INSERT INTO relay_tokens (token_hash, host_id, user_id) VALUES (%s, %s, %s)",
                        (token_hash, coordinate.host_id, coordinate.user_label),
                    )
        finally:
            conn.close()

    def deactivate_share(self, host_id: str, user_label: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shares SET state = 'inactive', updated_at = NOW() WHERE host_id = %s AND user_id = %s",
                        (host_id, user_label),
                    )
        finally:
            conn.close()

    def delete_relay_tokens(self, host_id: str, user_label: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM relay_tokens WHERE host_id = %s AND user_id = %s",
                        (host_id, user_label),
                    )
        finally:
            conn.close()

    def find_share_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT s.host_id, s.user_id, s.region, s.workspace_domain, s.state "
                    "FROM relay_tokens rt JOIN shares s ON s.host_id = rt.host_id AND s.user_id = rt.user_id "
                    "WHERE rt.token_hash = %s",
                    (token_hash,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "host_id": row[0],
            "user_id": row[1],
            "region": row[2],
            "workspace_domain": row[3],
            "state": row[4],
        }

    def find_active_share_by_workspace_domain(self, workspace_domain: str) -> dict[str, Any] | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SHARE_COLUMNS} FROM shares WHERE workspace_domain = %s AND state = 'active'",
                    (workspace_domain,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return _share_row_to_dict(row) if row is not None else None

    def record_tunnel_login(self, host_id: str, user_label: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shares SET last_tunnel_login_at = NOW(), updated_at = NOW() "
                        "WHERE host_id = %s AND user_id = %s",
                        (host_id, user_label),
                    )
        finally:
            conn.close()

    def get_pool_host_datacenter(self, host_id: str) -> str | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT region FROM pool_hosts WHERE host_id = %s ORDER BY created_at DESC LIMIT 1",
                    (host_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    def get_latest_cert_not_after(self, workspace_domain: str) -> str | None:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT not_after FROM issued_certs WHERE workspace_domain = %s ORDER BY not_after DESC LIMIT 1",
                    (workspace_domain,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return str(row[0]) if row is not None else None

    def count_certs_issued_in_last_day(self, host_id: str, user_label: str) -> int:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM issued_certs "
                    "WHERE host_id = %s AND user_id = %s AND created_at > NOW() - INTERVAL '24 hours'",
                    (host_id, user_label),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return int(row[0]) if row is not None else 0

    def record_issued_cert(
        self,
        workspace_domain: str,
        host_id: str,
        user_label: str,
        ca_name: str,
        cert_chain_pem: str,
        sans_json: str,
        not_after: str,
    ) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO issued_certs "
                        "(workspace_domain, host_id, user_id, ca_name, cert_chain_pem, sans, not_after) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (workspace_domain, host_id, user_label, ca_name, cert_chain_pem, sans_json, not_after),
                    )
        finally:
            conn.close()


@functools.cache
def get_share_store() -> ShareStore:
    return PostgresShareStore()


def require_share_user(request: Request) -> tuple[str, str]:
    """Authenticate a share endpoint caller and return (full SuperTokens user id, verified email).

    Share endpoints need the FULL user id (its hyphen-stripped form is a
    hostname label), which ``authenticate_request`` discards. A verified email
    is still required, exactly like every other user-authenticated endpoint.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    token = auth_header[7:]
    user_id = auth_module.get_user_id_from_access_token(token)
    email = auth_module.default_email_getter(user_id)
    if email is None:
        raise HTTPException(status_code=401, detail="Email not verified")
    return user_id, email


def _require_frps_plugin_secret(provided: str) -> None:
    """Authenticate an frps server-plugin callback against the fixed shared secret.

    The secret lives only in the relays' rendered ``frps.toml`` (delivered from
    Vault at provision time) and in the ``sharing-<env>`` Modal secret -- never
    in the desktop app or in workspaces. Raises 403 when the server has no
    secret configured (the plugin endpoint is disabled), 401 on mismatch.
    """
    expected = os.environ.get("FRPS_AUTH_SECRET", "")
    if not expected:
        raise HTTPException(status_code=403, detail="frps plugin auth is not enabled on this server")
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid frps plugin secret")


class CreateShareRequest(BaseModel):
    host_id: str = Field(description="The workspace's host coordinate (host-<32hex>) to share")


class FrpsAuthRequest(BaseModel):
    """The frps server-plugin op envelope: ``{version, op, content}``."""

    op: str
    content: dict[str, Any] = Field(default_factory=dict)


@router.post("/shares")
def create_share(request: Request, body: CreateShareRequest) -> dict[str, object]:
    """Enable sharing for one workspace: create (or reactivate) its share and mint a fresh relay token.

    Returns the workspace domain, the relay endpoint the workspace's frpc
    should dial, and the plaintext relay token -- returned exactly once here
    and never stored (only its hash is). Re-sharing an already-shared
    workspace reuses the share row and rotates the token.
    """
    with handle_endpoint_errors():
        user_id, _email = require_share_user(request)
        user_label = derive_share_user_label(user_id)
        store = get_share_store()
        datacenter = store.get_pool_host_datacenter(body.host_id)
        region = resolve_share_region(datacenter)
        coordinate = make_share_coordinate(
            host_id=body.host_id,
            user_label=user_label,
            region=region,
            content_domain=share_content_domain(),
        )
        relay_token = generate_relay_token()
        store.activate_share_and_rotate_token(
            coordinate, DEFAULT_MAX_SHARED_WORKSPACES_PER_USER, hash_relay_token(relay_token)
        )
        return {
            "host_id": coordinate.host_id,
            "workspace_domain": coordinate.workspace_domain,
            "region": region,
            "relay_endpoint": share_relay_endpoint_map()[region],
            "relay_token": relay_token,
        }


@router.get("/shares")
def list_shares(request: Request) -> dict[str, object]:
    """List all of the caller's share records (active and inactive)."""
    with handle_endpoint_errors():
        user_id, _email = require_share_user(request)
        user_label = derive_share_user_label(user_id)
        return {"shares": get_share_store().list_shares(user_label)}


@router.delete("/shares/{host_id}")
def delete_share(request: Request, host_id: str) -> dict[str, object]:
    """Disable sharing for one workspace: deactivate the share and delete its relay token.

    The share row is kept (state ``inactive``) for audit and fast re-share;
    the relay token is deleted, so the relay rejects the workspace's next
    tunnel Login/reconnect.
    """
    with handle_endpoint_errors():
        user_id, _email = require_share_user(request)
        user_label = derive_share_user_label(user_id)
        store = get_share_store()
        share = store.get_share(host_id, user_label)
        if share is None:
            raise ShareNotFoundError(host_id)
        store.deactivate_share(host_id, user_label)
        store.delete_relay_tokens(host_id, user_label)
        return {"host_id": host_id, "state": "inactive"}


@router.get("/shares/{host_id}/status")
def get_share_status(request: Request, host_id: str) -> dict[str, object]:
    """Report one share's state for the sharing UI: domain, tunnel liveness signal, cert expiry."""
    with handle_endpoint_errors():
        user_id, _email = require_share_user(request)
        user_label = derive_share_user_label(user_id)
        store = get_share_store()
        share = store.get_share(host_id, user_label)
        if share is None:
            raise ShareNotFoundError(host_id)
        relay_endpoint = share_relay_endpoint_map().get(str(share["region"]))
        cert_not_after = store.get_latest_cert_not_after(str(share["workspace_domain"]))
        return {
            "host_id": share["host_id"],
            "workspace_domain": share["workspace_domain"],
            "region": share["region"],
            "state": share["state"],
            "relay_endpoint": relay_endpoint,
            "last_tunnel_login_at": share["last_tunnel_login_at"],
            "cert_not_after": cert_not_after,
        }


@router.post("/frps/auth/{plugin_secret}")
def frps_auth(plugin_secret: str, body: FrpsAuthRequest) -> dict[str, object]:
    """Authorize an frps server-plugin operation (``Login`` / ``NewProxy``) for a relay.

    The relay's frps calls this for every workspace tunnel connect and hostname
    claim, authenticated by the shared secret embedded in its rendered plugin
    URL path. The presented relay token must resolve to an active share, and a
    ``NewProxy`` may only claim that share's own bare domain and wildcard.
    Every operation must present a relay token resolving to an active share
    (token-less bodies are rejected whatever the op); beyond that, operations
    other than the two we subscribe to are allowed unchanged -- frps should
    not be configured to send them, and constraining an unexpected op further
    would break the tunnel for no security gain.
    """
    with handle_endpoint_errors():
        _require_frps_plugin_secret(plugin_secret)
        relay_token = _extract_frps_relay_token(body.op, body.content)
        if relay_token is None:
            return _frps_reject("missing relay token").model_dump()
        store = get_share_store()
        share = store.find_share_by_token_hash(hash_relay_token(relay_token))
        if share is None or share["state"] != "active":
            return _frps_reject("unknown or inactive relay token").model_dump()
        if body.op == _FRPS_LOGIN_OP:
            store.record_tunnel_login(str(share["host_id"]), str(share["user_id"]))
            return _frps_allow().model_dump()
        if body.op == _FRPS_NEW_PROXY_OP:
            claimed_domains = _extract_frps_custom_domains(body.content)
            claimed_subdomain = _extract_frps_subdomain(body.content)
            return decide_frps_new_proxy(
                str(share["workspace_domain"]), claimed_domains, claimed_subdomain
            ).model_dump()
        return _frps_allow().model_dump()
