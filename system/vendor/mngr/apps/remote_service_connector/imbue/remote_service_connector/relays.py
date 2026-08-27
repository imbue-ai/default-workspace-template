"""Relay fleet inventory: the `relays` table and its operator admin API (/admin/relays).

The fleet is data, not env config (blueprint/multi-relay): each region runs N
relays and every shared workspace tunnels to all of the region's active relays
(phase 1 full replication). Share creation, the gateway assignment endpoint
(`GET /shares/assignment` in shares.py), and the health sweep (relay_health.py)
all read this table; the `share-relay` provisioning flow registers and retires
rows through the admin endpoints here.
"""

import functools
import hashlib
import ipaddress
import re
import secrets
from typing import Any
from typing import Final
from typing import Protocol

from fastapi import APIRouter
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

from imbue.remote_service_connector import db
from imbue.remote_service_connector.auth import require_admin_key
from imbue.remote_service_connector.errors import InvalidRelayRecordError
from imbue.remote_service_connector.errors import NoActiveRelaysError
from imbue.remote_service_connector.errors import RelayNotFoundError
from imbue.remote_service_connector.http_api import handle_endpoint_errors

router = APIRouter()

# Relay ids are opaque and non-secret; the shape is pinned so the id is safe to
# embed in the relay's rendered frps plugin-auth URL path.
_RELAY_ID_RE: Final[re.Pattern[str]] = re.compile(r"^relay-[a-f0-9]{8,32}$")

# A single DNS label (used to validate region codes), mirroring the shape rule
# in shares.py (duplicated rather than imported: shares.py imports this module).
_REGION_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^(?=.{1,63}$)[a-z0-9]+(?:-[a-z0-9]+)*$")

# host:port the workspace's frpc dials. The host part is an IP or DNS name;
# no scheme, no path.
_TUNNEL_ENDPOINT_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9.\-]+:[0-9]{1,5}$")

RELAY_HEALTHY: Final[str] = "healthy"
RELAY_UNHEALTHY: Final[str] = "unhealthy"

# Every relays SELECT returns these columns in this order.
_RELAY_COLUMNS: Final[str] = (
    "relay_id, region, tunnel_endpoint, ip_address, instance_name, is_active, health, consecutive_probe_failures"
)
_RELAY_COLUMN_NAMES: Final[tuple[str, ...]] = tuple(name.strip() for name in _RELAY_COLUMNS.split(","))


def generate_relay_id() -> str:
    """Mint a fresh opaque relay id (relay-<16 hex>)."""
    return f"relay-{secrets.token_hex(8)}"


def validate_relay_id(relay_id: str) -> str:
    normalized = relay_id.strip().lower()
    if _RELAY_ID_RE.match(normalized) is None:
        raise InvalidRelayRecordError(f"relay_id must be 'relay-<8..32 hex>', got {relay_id!r}")
    return normalized


def validate_relay_region(region: str) -> str:
    normalized = region.strip().lower()
    if _REGION_LABEL_RE.match(normalized) is None:
        raise InvalidRelayRecordError(f"region must be a lowercase DNS label, got {region!r}")
    return normalized


def validate_tunnel_endpoint(endpoint: str) -> str:
    normalized = endpoint.strip().lower()
    if _TUNNEL_ENDPOINT_RE.match(normalized) is None:
        raise InvalidRelayRecordError(f"tunnel_endpoint must be 'host:port', got {endpoint!r}")
    port = int(normalized.rpartition(":")[2])
    if not 1 <= port <= 65535:
        raise InvalidRelayRecordError(f"tunnel_endpoint port must be in 1..65535, got {endpoint!r}")
    return normalized


def validate_relay_ip_address(ip_address: str) -> str:
    """The registered IP becomes a DNS A-record answer, so it must be a literal IPv4 address."""
    normalized = ip_address.strip()
    try:
        ipaddress.IPv4Address(normalized)
    except ValueError:
        raise InvalidRelayRecordError(f"ip_address must be a literal IPv4 address, got {ip_address!r}") from None
    return normalized


def eligible_regions(relay_rows: list[dict[str, Any]]) -> list[str]:
    """The regions a fresh share may be placed in: those with at least one active relay, sorted."""
    return sorted({str(row["region"]) for row in relay_rows if row["is_active"]})


def pick_fallback_region(host_id: str, regions: list[str]) -> str:
    """Deterministically spread latency-unknown shares over the eligible regions.

    Replaces the old SHARE_DEFAULT_REGION env var: hashing the host id spreads
    load instead of piling every latency-unknown share onto one configured
    region, stays stable per host (re-tries land in the same region), and
    degenerates correctly on single-region tiers. Raises
    :class:`NoActiveRelaysError` on an empty region list.
    """
    if not regions:
        raise NoActiveRelaysError(None)
    ordered_regions = sorted(regions)
    digest = int(hashlib.sha256(host_id.encode("utf-8")).hexdigest(), 16)
    return ordered_regions[digest % len(ordered_regions)]


def relay_endpoints_for_region(relay_rows: list[dict[str, Any]], region: str) -> list[dict[str, str]]:
    """The assignment payload entries for one region: every active relay, stable order.

    Phase 1 is full replication (every share tunnels to every relay in its
    region); phase 2 narrows this to the share's bucket owners without
    changing the payload shape. Health is deliberately ignored here: an
    unhealthy relay should keep receiving tunnels so it serves again the
    moment it recovers -- health only filters DNS answers.
    """
    return [
        {"relay_id": str(row["relay_id"]), "endpoint": str(row["tunnel_endpoint"])}
        for row in sorted(relay_rows, key=lambda relay_row: str(relay_row["relay_id"]))
        if row["is_active"] and str(row["region"]) == region
    ]


class RelayStore(Protocol):
    """Abstraction over the relays / share_tunnel_logins tables so endpoints are unit-testable."""

    def list_relays(self) -> list[dict[str, Any]]: ...
    def upsert_relay(
        self, relay_id: str, region: str, tunnel_endpoint: str, ip_address: str, instance_name: str
    ) -> None: ...
    def retire_relay(self, relay_id: str) -> bool: ...
    def update_relay_health(self, relay_id: str, health: str, consecutive_probe_failures: int) -> None: ...
    def record_relay_login(self, host_id: str, user_label: str, relay_id: str) -> None: ...
    def list_share_relay_logins(self, host_id: str, user_label: str) -> list[dict[str, Any]]: ...


def _relay_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_RELAY_COLUMN_NAMES, row, strict=True))


class PostgresRelayStore:
    """RelayStore backed by the connector's existing Neon DB."""

    def list_relays(self) -> list[dict[str, Any]]:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_RELAY_COLUMNS} FROM relays ORDER BY relay_id")
                rows = cur.fetchall()
        finally:
            conn.close()
        return [_relay_row_to_dict(row) for row in rows]

    def upsert_relay(
        self, relay_id: str, region: str, tunnel_endpoint: str, ip_address: str, instance_name: str
    ) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Re-registering revives a retired row: a replacement deploy
                    # under the same id must come back active and healthy.
                    cur.execute(
                        "INSERT INTO relays (relay_id, region, tunnel_endpoint, ip_address, instance_name) "
                        "VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (relay_id) DO UPDATE SET "
                        "region = EXCLUDED.region, tunnel_endpoint = EXCLUDED.tunnel_endpoint, "
                        "ip_address = EXCLUDED.ip_address, instance_name = EXCLUDED.instance_name, "
                        "is_active = TRUE, health = 'healthy', consecutive_probe_failures = 0, "
                        "updated_at = NOW()",
                        (relay_id, region, tunnel_endpoint, ip_address, instance_name),
                    )
        finally:
            conn.close()

    def retire_relay(self, relay_id: str) -> bool:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE relays SET is_active = FALSE, updated_at = NOW() WHERE relay_id = %s",
                        (relay_id,),
                    )
                    return bool(cur.rowcount)
        finally:
            conn.close()

    def update_relay_health(self, relay_id: str, health: str, consecutive_probe_failures: int) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE relays SET health = %s, consecutive_probe_failures = %s, updated_at = NOW() "
                        "WHERE relay_id = %s",
                        (health, consecutive_probe_failures, relay_id),
                    )
        finally:
            conn.close()

    def record_relay_login(self, host_id: str, user_label: str, relay_id: str) -> None:
        conn = db.get_pool_db_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO share_tunnel_logins (host_id, user_id, relay_id) VALUES (%s, %s, %s) "
                        "ON CONFLICT (host_id, user_id, relay_id) DO UPDATE SET last_login_at = NOW()",
                        (host_id, user_label, relay_id),
                    )
        finally:
            conn.close()

    def list_share_relay_logins(self, host_id: str, user_label: str) -> list[dict[str, Any]]:
        conn = db.get_pool_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT relay_id, last_login_at FROM share_tunnel_logins "
                    "WHERE host_id = %s AND user_id = %s ORDER BY relay_id",
                    (host_id, user_label),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [{"relay_id": row[0], "last_login_at": str(row[1]) if row[1] is not None else None} for row in rows]


@functools.cache
def get_relay_store() -> RelayStore:
    return PostgresRelayStore()


class RegisterRelayRequest(BaseModel):
    """Body for POST /admin/relays (register or update one relay)."""

    relay_id: str | None = Field(
        default=None, description="Existing relay id to update; omit to mint a fresh one (relay-<hex>)"
    )
    region: str = Field(description="Region code the relay serves (e.g. us1)")
    tunnel_endpoint: str = Field(description="host:port the workspaces' frpc dials (typically <ip>:7000)")
    ip_address: str = Field(description="Public IPv4 (DNS answer for the region wildcard + healthz probe target)")
    instance_name: str = Field(default="", description="Human-readable instance name (share-relay-<env>-<region>-<n>)")

    @field_validator("relay_id")
    @classmethod
    def _validate_relay_id(cls, value: str | None) -> str | None:
        return None if value is None else validate_relay_id(value)

    @field_validator("region")
    @classmethod
    def _validate_region(cls, value: str) -> str:
        return validate_relay_region(value)

    @field_validator("tunnel_endpoint")
    @classmethod
    def _validate_tunnel_endpoint(cls, value: str) -> str:
        return validate_tunnel_endpoint(value)

    @field_validator("ip_address")
    @classmethod
    def _validate_ip_address(cls, value: str) -> str:
        return validate_relay_ip_address(value)


def _relay_row_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "relay_id": row["relay_id"],
        "region": row["region"],
        "tunnel_endpoint": row["tunnel_endpoint"],
        "ip_address": row["ip_address"],
        "instance_name": row["instance_name"],
        "is_active": bool(row["is_active"]),
        "health": row["health"],
        "consecutive_probe_failures": int(row["consecutive_probe_failures"]),
    }


@router.get("/admin/relays")
def list_relays(request: Request) -> dict[str, object]:
    """List every relay row (active and retired), admin-key authenticated."""
    with handle_endpoint_errors():
        require_admin_key(request)
        return {"relays": [_relay_row_public(row) for row in get_relay_store().list_relays()]}


@router.post("/admin/relays")
def register_relay(request: Request, body: RegisterRelayRequest) -> dict[str, object]:
    """Register (or update) one relay; the provisioning flow's final step.

    Idempotent upsert by relay id: re-registering revives a retired row and
    resets its health, so a replacement deploy under a kept id just works.
    Registration makes the relay share-eligible immediately; DNS converges on
    the next health-sweep pass (or via `share-relay dns` during bring-up).
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        relay_id = body.relay_id if body.relay_id is not None else generate_relay_id()
        store = get_relay_store()
        store.upsert_relay(
            relay_id=relay_id,
            region=body.region,
            tunnel_endpoint=body.tunnel_endpoint,
            ip_address=body.ip_address,
            instance_name=body.instance_name,
        )
        row = next(relay_row for relay_row in store.list_relays() if relay_row["relay_id"] == relay_id)
        return _relay_row_public(row)


@router.delete("/admin/relays/{relay_id}")
def retire_relay(request: Request, relay_id: str) -> dict[str, object]:
    """Retire one relay: it leaves assignment, DNS, and frps auth; the row is kept for audit.

    DNS caveat: the health sweep only reconciles regions with at least one
    ACTIVE relay, so retiring a region's last relay leaves its record sets
    untouched (the cron never empties a region's DNS answer) -- clean those up
    manually with `share-relay dns` when decommissioning a whole region.
    """
    with handle_endpoint_errors():
        require_admin_key(request)
        normalized_relay_id = validate_relay_id(relay_id)
        if not get_relay_store().retire_relay(normalized_relay_id):
            raise RelayNotFoundError(normalized_relay_id)
        return {"relay_id": normalized_relay_id, "is_active": False}
