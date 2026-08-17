"""Registering relays with the connector's fleet inventory (/admin/relays).

The connector's ``relays`` table is the source of truth for the fleet: share
creation, the workspace assignment endpoint, frps auth, and the health-driven
DNS reconciliation all read it. The provisioning flow finishes by registering
the new relay here (and ``destroy`` deregisters), so a deployed relay is
share-eligible without any further configuration.
"""

from typing import Any
from typing import Final

import httpx

from imbue.share_relay.errors import ShareRelayError
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.primitives import RelayId

_ADMIN_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0


class RelayRegistrationError(ShareRelayError):
    """Raised when the connector's relay admin API refuses or fails a registration call."""


def _check_admin_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RelayRegistrationError(f"Connector relay admin API error {response.status_code}: {response.text[:500]}")
    try:
        body = response.json()
    except ValueError as exc:
        # Edge proxies can answer with HTML or an empty body; surface the
        # status rather than an opaque JSON decode error.
        raise RelayRegistrationError(
            f"Connector relay admin API returned a non-JSON response (status {response.status_code})"
        ) from exc
    if not isinstance(body, dict):
        raise RelayRegistrationError(f"Connector relay admin API returned a non-object body: {body!r}")
    return body


def register_relay(
    connector_url: str,
    admin_key: str,
    # None registers a fresh relay (the connector mints the id); a value
    # re-registers / revives that relay in place.
    relay_id: RelayId | None,
    region: RegionCode,
    tunnel_endpoint: str,
    ip_address: str,
    instance_name: str,
) -> dict[str, Any]:
    """Register (or update) one relay row; returns the connector's relay record (with its relay_id)."""
    body: dict[str, Any] = {
        "region": str(region),
        "tunnel_endpoint": tunnel_endpoint,
        "ip_address": ip_address,
        "instance_name": instance_name,
    }
    if relay_id is not None:
        body["relay_id"] = str(relay_id)
    response = httpx.post(
        f"{connector_url.rstrip('/')}/admin/relays",
        json=body,
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=_ADMIN_REQUEST_TIMEOUT_SECONDS,
    )
    return _check_admin_response(response)


def deregister_relay(connector_url: str, admin_key: str, relay_id: RelayId) -> dict[str, Any]:
    """Retire one relay row (it leaves assignment, DNS, and frps auth)."""
    response = httpx.delete(
        f"{connector_url.rstrip('/')}/admin/relays/{relay_id}",
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=_ADMIN_REQUEST_TIMEOUT_SECONDS,
    )
    return _check_admin_response(response)
