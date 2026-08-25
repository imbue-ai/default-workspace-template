"""Self-hosted sharing: share records, relay tokens, and frps plugin auth (/shares/*, /frps/auth).

Replaces the Cloudflare tunnel/Access sharing model (blueprint/sharing-redesign).
A shared workspace lives at
``<service>.<host-id>.<user-label>.<region>.<content-domain>``: the bare
``<host-id>.<user-label>.<region>.<content-domain>`` is the workspace shell,
and ``<user-label>.<region>.<content-domain>`` is the registrable-site
boundary (the Public-Suffix-List entry is ``<region>.<content-domain>``).
The workspace's frpc claims explicit per-service labels directly under the
bare domain on a relay (plus a dedicated auth label), so services route
without per-service DNS records while the relay only routes hostnames the
workspace was authorized to claim (see ``decide_frps_new_proxy``). Ids are
full and untruncated: host
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
from collections.abc import Callable
from typing import Any
from typing import Protocol

import psycopg2
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

import imbue.remote_service_connector.accounts_web as accounts_web_module
import imbue.remote_service_connector.relays as relays_module
from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector import db
from imbue.remote_service_connector.errors import InvalidShareCoordinateError
from imbue.remote_service_connector.errors import MissingShareConfigError
from imbue.remote_service_connector.errors import NoActiveRelaysError
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
_FRPS_PING_OP = "Ping"

# frpc carries its relay token in the client metadata map under this key
# (``metadatas.relay_token`` in frpc.toml). Login ops receive the map as
# ``content.metas``; NewProxy ops receive it nested under ``content.user.metas``.
_FRPS_RELAY_TOKEN_META_KEY = "relay_token"

# OVH datacenter code prefix -> region code. us1 is west (Hillsboro), us2 is
# east (Vint Hill). A datacenter that maps to a region with no active relay
# (and any unknown datacenter) falls back to a deterministic spread over the
# share-eligible regions (see relays.pick_fallback_region).
_SHARE_DATACENTER_REGION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("US-WEST", "us1"),
    ("US-EAST", "us2"),
)

# How often the in-workspace share gateway re-polls GET /shares/assignment.
# Server-controlled so fleet-change convergence can be tuned without touching
# workspaces.
ASSIGNMENT_POLL_SECONDS = 60


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
        """The wildcard SAN the workspace's certificate covers its per-service labels with.

        Cert-only: the relay never routes the wildcard itself -- frpc claims
        explicit per-service labels and ``decide_frps_new_proxy`` rejects
        wildcard claims.
        """
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


def resolve_share_region(datacenter: str | None, eligible_regions: list[str], host_id: str) -> str:
    """Pick the region code for a fresh share: the host's datacenter's region when a relay serves it, else spread.

    ``datacenter`` is the pool host's OVH DC code (e.g. ``US-EAST-VA``), or None
    for hosts the connector has no record of (local workspaces). Latency-unknown
    shares fall back to a deterministic hash-of-host-id spread over the
    share-eligible regions (single-region dev tiers degenerate to that region).
    """
    if not eligible_regions:
        raise NoActiveRelaysError(None)
    if datacenter:
        datacenter_upper = datacenter.upper()
        for prefix, region in _SHARE_DATACENTER_REGION_PREFIXES:
            if datacenter_upper.startswith(prefix) and region in eligible_regions:
                return region
    return relays_module.pick_fallback_region(host_id, eligible_regions)


def resolve_share_region_for_share(
    existing_region: str | None,
    datacenter: str | None,
    preferred_region: str | None,
    eligible_regions: list[str],
    host_id: str,
) -> str:
    """Pick the region for one share bring-up, sticky on the share's existing row.

    The region is baked into the workspace domain (DNS, PSL boundary, cert
    SANs, session cookies), so a re-share must never silently move it: an
    existing row's region wins as long as a relay still serves it. A fresh
    share prefers the host's datacenter mapping (pool hosts); a host the
    connector has no datacenter record of (a local workspace) may instead be
    steered by the caller's ``preferred_region`` -- the desktop measures its
    own latency to each relay, which is the best proximity signal available
    for a workspace running on the user's machine. Unknown preferred regions
    are ignored (not errors), so a stale client never breaks on fleet changes.
    """
    if not eligible_regions:
        raise NoActiveRelaysError(None)
    if existing_region is not None and existing_region in eligible_regions:
        return existing_region
    if datacenter is None and preferred_region is not None and preferred_region in eligible_regions:
        return preferred_region
    return resolve_share_region(datacenter, eligible_regions, host_id)


def active_relay_rows() -> list[dict[str, Any]]:
    """Every active relay row, from the relays table (the fleet's source of truth)."""
    return [row for row in relays_module.get_relay_store().list_relays() if row["is_active"]]


def relay_endpoints_for_share_region(relay_rows: list[dict[str, Any]], region: str) -> list[dict[str, str]]:
    """The relay endpoint entries a share in ``region`` tunnels to; raises when the region has none."""
    entries = relays_module.relay_endpoints_for_region(relay_rows, region)
    if not entries:
        raise NoActiveRelaysError(region)
    return entries


class FrpsAuthDecision(BaseModel):
    """The reply the connector returns to the frps server plugin.

    ``reject`` rejects the operation with ``reject_reason``; otherwise the
    operation proceeds unchanged (``unchange = True``). frps calls this only
    for the subscribed ops (``Login``, ``NewProxy``, ``Ping``) -- never for
    visitor connections -- so a shared workspace's actual traffic never
    reaches the connector.
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


def decide_frps_ping(
    share_lookup: Callable[[str], dict[str, Any] | None], relay_token: str | None
) -> FrpsAuthDecision:
    """Authorize an frps ``Ping`` heartbeat: reject only on an affirmative non-active share.

    Rejecting a ping makes frpc close its whole session (frps answers the
    heartbeat with an error Pong), which is exactly how a suspended or
    unshared workspace's LIVE tunnel is severed within one heartbeat interval
    (~10s). But frp also fails closed on plugin errors, so this path fails
    OPEN on the connector's own internal errors: tunnel uptime stays coupled
    only to the connector being reachable, and a non-active share slips
    through only until the next successful lookup. ``Login``/``NewProxy``
    keep their fail-closed handling -- they are security decisions, while a
    heartbeat merely continues an already-authorized session.
    """
    if relay_token is None:
        return _frps_reject("missing relay token")
    try:
        share = share_lookup(hash_relay_token(relay_token))
    except psycopg2.Error as exc:
        emit_metric("frps_ping_fail_open", 1, {})
        logger.warning("Allowing frps ping despite a share lookup failure", exc_info=exc)
        return _frps_allow()
    if share is None or share["state"] != "active":
        emit_metric("frps_ping_rejected", 1, {})
        return _frps_reject("unknown or inactive relay token")
    return _frps_allow()


def _extract_frps_relay_token(op: str, content: dict[str, Any]) -> str | None:
    """Pull the relay token out of an frps plugin op's metadata map.

    Login ops carry the frpc client metadata at ``content.metas``; NewProxy,
    Ping, and the other client-scoped ops nest it under ``content.user.metas``.
    These shapes are verified against frp 0.70.1 (the pinned relay release): a
    Login body is ``{op, content: {metas: {relay_token}, ...}}``, a NewProxy
    body is ``{op, content: {user: {metas: {relay_token}}, custom_domains:
    [...], ...}}``, and a Ping body is ``{op, content: {user: {metas:
    {relay_token}, run_id, ...}, ping: {...}}}``.
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
_SHARE_COLUMNS = (
    "host_id, user_id, region, workspace_domain, state, created_at, updated_at, last_tunnel_login_at, entry_label"
)
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
        self, coordinate: ShareCoordinate, max_active_shares: int, token_hash: str, entry_label: str | None
    ) -> None: ...
    def update_share_entry_label(self, host_id: str, user_label: str, entry_label: str) -> None: ...
    def deactivate_share(self, host_id: str, user_label: str) -> None: ...
    def suspend_shares_for_user(self, user_label: str) -> int: ...
    def unsuspend_shares_for_user(self, user_label: str) -> int: ...
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
        self, coordinate: ShareCoordinate, max_active_shares: int, token_hash: str, entry_label: str | None
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
                    # A caller that could not learn the entry label (e.g. the
                    # desktop's client-side flow) must not erase one a previous
                    # bring-up recorded, hence the COALESCE.
                    cur.execute(
                        "INSERT INTO shares (host_id, user_id, region, workspace_domain, state, entry_label) "
                        "VALUES (%s, %s, %s, %s, 'active', %s) "
                        "ON CONFLICT (host_id, user_id) DO UPDATE SET "
                        "region = EXCLUDED.region, workspace_domain = EXCLUDED.workspace_domain, "
                        "state = 'active', updated_at = NOW(), "
                        "entry_label = COALESCE(EXCLUDED.entry_label, shares.entry_label)",
                        (
                            coordinate.host_id,
                            coordinate.user_label,
                            coordinate.region,
                            coordinate.workspace_domain,
                            entry_label,
                        ),
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

    def update_share_entry_label(self, host_id: str, user_label: str, entry_label: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shares SET entry_label = %s, updated_at = NOW() WHERE host_id = %s AND user_id = %s",
                        (entry_label, host_id, user_label),
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

    def suspend_shares_for_user(self, user_label: str) -> int:
        """Flip every active share of one user to ``suspended``, keeping the relay tokens.

        The retained token rows are what make unsuspension self-healing: the
        workspace still holds the plaintext token, so once the share is back
        to ``active`` its next tunnel login succeeds with no re-share.
        """
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shares SET state = 'suspended', updated_at = NOW() "
                        "WHERE user_id = %s AND state = 'active'",
                        (user_label,),
                    )
                    return cur.rowcount
        finally:
            conn.close()

    def unsuspend_shares_for_user(self, user_label: str) -> int:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE shares SET state = 'active', updated_at = NOW() "
                        "WHERE user_id = %s AND state = 'suspended'",
                        (user_label,),
                    )
                    return cur.rowcount
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


def _require_share_user(request: Request) -> str:
    """Authenticate a share endpoint caller and return the full SuperTokens user id.

    Share endpoints need the FULL user id (its hyphen-stripped form is a
    hostname label), which ``authenticate_request`` discards. A Bearer header
    wins (the desktop / CLI path); otherwise the hosted chrome's browser
    session cookie is resolved (it reads share status for its health badges).
    Owning/managing a share does not require a verified email -- only
    *visiting* one does (the email is the visitor's authorization identity,
    enforced by the accounts broker).
    """
    return accounts_web_module.resolve_web_user_identity(request)[1]


def require_active_share_by_relay_token(request: Request) -> dict[str, Any]:
    """Authenticate a workspace-side endpoint by its share's relay token (Bearer) and return the share row.

    The relay token is the credential the workspace holds (delivered in
    share.env); both the cert-issuance endpoint and the gateway assignment
    endpoint authenticate with it. Raises 401 on a missing bearer header or a
    token that does not resolve to an active share.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer credentials")
    relay_token = auth_header[7:]
    share = get_share_store().find_share_by_token_hash(hash_relay_token(relay_token))
    if share is None or share["state"] != "active":
        raise HTTPException(status_code=401, detail="Unknown or inactive relay token")
    return share


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


# A service origin label: the hostname component in front of the workspace
# domain (e.g. ``system_interface-elm7wydc``). Underscores appear in real
# service names, so they are allowed alongside DNS label characters.
_ENTRY_LABEL_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,62}$")


def normalize_entry_label(value: str) -> str | None:
    """The normalized (stripped, lowercased) entry label, or None when it is not a single origin label.

    The shape rule for every entry-label source: client-supplied share creates
    and the labels recorded from frps ``NewProxy`` claims. The label is
    interpolated into ``https://<label>.<workspace domain>/`` URLs by the
    hosted chrome, so anything beyond one hostname label must be refused.
    """
    stripped = value.strip().lower()
    if not _ENTRY_LABEL_RE.match(stripped):
        return None
    return stripped


# The workspace shell service, whose origin label is the hosted chrome's entry
# point into a share. frpc's NewProxy claims carry the workspace's own service
# labels, so an allowed claim is where the connector learns the entry label --
# with no access into the workspace. The bare name (no ``-<rand>`` suffix)
# covers legacy labels that predate the random-suffix scheme.
_ENTRY_SERVICE_LABEL_NAME = "system_interface"


def entry_label_from_claimed_domains(workspace_domain: str, claimed_custom_domains: list[str]) -> str | None:
    """The shell service's origin label among a NewProxy claim's custom domains, or None."""
    suffix = "." + workspace_domain.lower()
    for claimed in claimed_custom_domains:
        normalized_claim = claimed.strip().lower()
        if not normalized_claim.endswith(suffix):
            continue
        label = normalized_claim[: -len(suffix)]
        if label == _ENTRY_SERVICE_LABEL_NAME or label.startswith(_ENTRY_SERVICE_LABEL_NAME + "-"):
            return normalize_entry_label(label)
    return None


class CreateShareRequest(BaseModel):
    host_id: str = Field(description="The workspace's host coordinate (host-<32hex>) to share")
    entry_label: str | None = Field(
        default=None,
        description=(
            "The workspace's shell-service origin label (e.g. system_interface-<rand>). The bare "
            "workspace domain is deliberately unrouted on the relay, so this is the routable origin "
            "the hosted web chrome enters and health-probes the workspace at. Optional: omitting it "
            "keeps any previously recorded value."
        ),
    )

    preferred_region: str | None = Field(
        default=None,
        description=(
            "Preferred relay region code (e.g. us1). Honored only for hosts the connector has no "
            "datacenter record of (local workspaces) and only when a relay serves that region; a "
            "re-share always keeps the share's existing region. Clients typically pick this by "
            "measuring their own latency to each relay from GET /shares/relays."
        ),
    )

    @field_validator("entry_label")
    @classmethod
    def _validate_entry_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_entry_label(value)
        if normalized is None:
            raise InvalidShareCoordinateError(f"entry_label must be a single origin label, got {value!r}")
        return normalized

    @field_validator("preferred_region")
    @classmethod
    def _validate_preferred_region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip().lower()
        if _SHARE_DNS_LABEL_RE.match(stripped) is None:
            raise InvalidShareCoordinateError(f"preferred_region must be a DNS label, got {value!r}")
        return stripped


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
        user_id = _require_share_user(request)
        user_label = derive_share_user_label(user_id)
        store = get_share_store()
        relay_rows = active_relay_rows()
        existing_share = store.get_share(body.host_id, user_label)
        region = resolve_share_region_for_share(
            existing_region=str(existing_share["region"]) if existing_share is not None else None,
            datacenter=store.get_pool_host_datacenter(body.host_id),
            preferred_region=body.preferred_region,
            eligible_regions=relays_module.eligible_regions(relay_rows),
            host_id=body.host_id,
        )
        coordinate = make_share_coordinate(
            host_id=body.host_id,
            user_label=user_label,
            region=region,
            content_domain=share_content_domain(),
        )
        relay_endpoints = relay_endpoints_for_share_region(relay_rows, region)
        relay_token = generate_relay_token()
        store.activate_share_and_rotate_token(
            coordinate, DEFAULT_MAX_SHARED_WORKSPACES_PER_USER, hash_relay_token(relay_token), body.entry_label
        )
        return {
            "host_id": coordinate.host_id,
            "workspace_domain": coordinate.workspace_domain,
            "region": region,
            "relay_endpoints": relay_endpoints,
            "relay_token": relay_token,
        }


@router.get("/shares/relays")
def list_share_relays(request: Request) -> dict[str, object]:
    """The region -> relay tunnel-control endpoints map (every active relay per region).

    Lets clients pick a ``preferred_region`` for a local workspace's share by
    measuring their own latency to each relay's tunnel-control endpoint
    (scoring a region by its best endpoint).
    """
    with handle_endpoint_errors():
        _require_share_user(request)
        relay_rows = active_relay_rows()
        endpoints_by_region: dict[str, list[str]] = {}
        for region in relays_module.eligible_regions(relay_rows):
            endpoints_by_region[region] = [
                entry["endpoint"] for entry in relays_module.relay_endpoints_for_region(relay_rows, region)
            ]
        return {"relays": endpoints_by_region}


@router.get("/shares/assignment")
def get_share_assignment(request: Request) -> dict[str, object]:
    """The relay endpoint set this share's workspace should tunnel to right now.

    Authenticated by the share's relay token (Bearer) -- the same credential
    the workspace already holds for cert issuance. The in-workspace share
    gateway calls this at stack start and re-polls every ``poll_seconds`` (and
    on frpc failure), converging its frpc set on the answer; fleet changes
    therefore never require touching workspaces. The response is cached on
    disk in the workspace so a container restart works with the connector down.
    """
    with handle_endpoint_errors():
        share = require_active_share_by_relay_token(request)
        relay_endpoints = relay_endpoints_for_share_region(active_relay_rows(), str(share["region"]))
        return {
            "workspace_domain": share["workspace_domain"],
            "relay_endpoints": relay_endpoints,
            "poll_seconds": ASSIGNMENT_POLL_SECONDS,
        }


@router.get("/shares")
def list_shares(request: Request) -> dict[str, object]:
    """List all of the caller's share records (active and inactive)."""
    with handle_endpoint_errors():
        user_id = _require_share_user(request)
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
        user_id = _require_share_user(request)
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
        user_id = _require_share_user(request)
        user_label = derive_share_user_label(user_id)
        store = get_share_store()
        share = store.get_share(host_id, user_label)
        if share is None:
            raise ShareNotFoundError(host_id)
        # A region that lost all its relays reports an empty endpoint list here
        # (status is a read; only share bring-up hard-fails on a relay-less region).
        relay_endpoints = relays_module.relay_endpoints_for_region(active_relay_rows(), str(share["region"]))
        cert_not_after = store.get_latest_cert_not_after(str(share["workspace_domain"]))
        relay_logins = relays_module.get_relay_store().list_share_relay_logins(host_id, user_label)
        return {
            "host_id": share["host_id"],
            "workspace_domain": share["workspace_domain"],
            "region": share["region"],
            "state": share["state"],
            "relay_endpoints": relay_endpoints,
            "last_tunnel_login_at": share["last_tunnel_login_at"],
            "relays": relay_logins,
            "cert_not_after": cert_not_after,
            "entry_label": share.get("entry_label"),
        }


@router.post("/frps/auth/{plugin_secret}/{relay_id}")
def frps_auth(plugin_secret: str, relay_id: str, body: FrpsAuthRequest) -> dict[str, object]:
    """Authorize an frps server-plugin operation (``Login`` / ``NewProxy`` / ``Ping``) for one relay.

    The relay's frps calls this for every workspace tunnel connect, hostname
    claim, and heartbeat, authenticated by the shared secret embedded in its
    rendered plugin URL path; the path's trailing relay id identifies WHICH
    relay is calling, so tunnel logins are attributable per relay (the
    fleet-convergence signal). The presented relay token must resolve to an
    active share; a ``NewProxy`` may only claim single per-service labels
    directly under that share's own domain (see ``decide_frps_new_proxy``),
    and a ``Ping`` whose token no longer resolves to an active share is
    rejected fail-open (see ``decide_frps_ping``) -- the live-tunnel kill
    switch. Every operation must present a relay token resolving to an active
    share (token-less bodies are rejected whatever the op); beyond that,
    operations other than the ones we subscribe to are allowed unchanged --
    frps should not be configured to send them, and constraining an
    unexpected op further would break the tunnel for no security gain.
    """
    with handle_endpoint_errors():
        _require_frps_plugin_secret(plugin_secret)
        if body.op == _FRPS_PING_OP:
            # Heartbeats skip the relay-row lookup: the plugin secret already
            # authenticates the caller, and a DB read here would couple every
            # live tunnel to the relays table (see decide_frps_ping's
            # fail-open rationale).
            return decide_frps_ping(
                get_share_store().find_share_by_token_hash,
                _extract_frps_relay_token(body.op, body.content),
            ).model_dump()
        relay_row = next(
            (row for row in active_relay_rows() if str(row["relay_id"]) == relay_id),
            None,
        )
        if relay_row is None:
            return _frps_reject(f"unknown or retired relay id {relay_id!r}").model_dump()
        relay_token = _extract_frps_relay_token(body.op, body.content)
        if relay_token is None:
            return _frps_reject("missing relay token").model_dump()
        store = get_share_store()
        share = store.find_share_by_token_hash(hash_relay_token(relay_token))
        if share is None or share["state"] != "active":
            return _frps_reject("unknown or inactive relay token").model_dump()
        if body.op == _FRPS_LOGIN_OP:
            store.record_tunnel_login(str(share["host_id"]), str(share["user_id"]))
            relays_module.get_relay_store().record_relay_login(str(share["host_id"]), str(share["user_id"]), relay_id)
            return _frps_allow().model_dump()
        if body.op == _FRPS_NEW_PROXY_OP:
            claimed_domains = _extract_frps_custom_domains(body.content)
            claimed_subdomain = _extract_frps_subdomain(body.content)
            decision = decide_frps_new_proxy(str(share["workspace_domain"]), claimed_domains, claimed_subdomain)
            # An allowed claim carries the workspace's own service labels, so
            # this is where the connector learns the chrome's entry origin --
            # without ever reaching into the workspace. Recorded on every
            # claim so a re-registered shell label self-heals the share row.
            if not decision.reject:
                entry_label = entry_label_from_claimed_domains(str(share["workspace_domain"]), claimed_domains)
                if entry_label is not None:
                    store.update_share_entry_label(str(share["host_id"]), str(share["user_id"]), entry_label)
            return decision.model_dump()
        return _frps_allow().model_dump()
