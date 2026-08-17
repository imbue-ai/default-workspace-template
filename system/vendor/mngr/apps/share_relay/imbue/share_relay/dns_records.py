"""Cloudflare DNS record sets for a region's relay fleet.

The content wildcard ``*.<region>.<content-domain>`` answers with EVERY relay
IP in the region (browsers fall back to the next A record when one relay's
TCP connect fails, which is the fast failover path);
``relay.<region>.<content-domain>`` carries the same set as the human-facing
relay name. Records are gray-cloud (DNS only, ``proxied=false``): the relays
do SNI passthrough, so Cloudflare must not terminate TLS in front of them.

This CLI-side reconciliation covers bring-up and disaster recovery; in steady
state the connector's relay_health_sweep cron maintains the same record sets
from the relays table (health-filtered).
"""

from typing import Any
from typing import Final

import httpx

from imbue.imbue_common.pure import pure
from imbue.share_relay.errors import ShareRelayError

_CF_BASE_URL: Final[str] = "https://api.cloudflare.com/client/v4"
_DNS_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

# Low TTL so a health-driven pull (or a bring-up repoint) propagates quickly.
RELAY_DNS_TTL_SECONDS: Final[int] = 60


class RelayDnsError(ShareRelayError):
    """Raised when a Cloudflare DNS record for the relay fleet cannot be reconciled."""


@pure
def relay_dns_record_names(region_domain: str) -> list[str]:
    """The record names one region needs: its relay host name and the content wildcard."""
    return [f"relay.{region_domain}", f"*.{region_domain}"]


def _check_cf(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        # Edge proxies can answer with HTML (502/504) or an empty body; surface
        # the status rather than an opaque JSON decode error.
        raise RelayDnsError(f"Cloudflare DNS API returned non-JSON response (status {response.status_code})") from exc
    if response.status_code >= 400 or not body.get("success", False):
        raise RelayDnsError(f"Cloudflare DNS API error {response.status_code}: {body.get('errors')}")
    return body


def reconcile_a_record_set(client: httpx.Client, zone_id: str, record_name: str, ip_addresses: list[str]) -> bool:
    """Make the name's gray-cloud A records exactly ``ip_addresses``; returns whether anything changed."""
    if not ip_addresses:
        raise RelayDnsError(f"Refusing to reconcile {record_name!r} to an empty IP set")
    listing = _check_cf(
        client.get(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records", params={"type": "A", "name": record_name})
    )
    existing_id_by_ip = {str(record["content"]): str(record["id"]) for record in listing.get("result", [])}
    desired = set(ip_addresses)
    is_changed = False
    for ip_address in sorted(desired - set(existing_id_by_ip)):
        record_body = {
            "type": "A",
            "name": record_name,
            "content": ip_address,
            "ttl": RELAY_DNS_TTL_SECONDS,
            "proxied": False,
        }
        _check_cf(client.post(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records", json=record_body))
        is_changed = True
    for stale_ip in sorted(set(existing_id_by_ip) - desired):
        _check_cf(client.delete(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records/{existing_id_by_ip[stale_ip]}"))
        is_changed = True
    return is_changed


def reconcile_relay_dns_records(
    api_token: str, zone_id: str, region_domain: str, ip_addresses: list[str]
) -> list[str]:
    """Point the region's record set at exactly ``ip_addresses``; returns the reconciled record names."""
    with httpx.Client(
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=_DNS_REQUEST_TIMEOUT_SECONDS,
    ) as client:
        record_names = relay_dns_record_names(region_domain)
        for record_name in record_names:
            reconcile_a_record_set(client, zone_id, record_name, ip_addresses)
        return record_names
