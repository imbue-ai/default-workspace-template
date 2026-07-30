"""Cloudflare DNS records for a relay: the region's content wildcard + the relay's own name.

The content wildcard ``*.<region>.<content-domain>`` sends every shared
workspace hostname in the region to the relay's IP; ``relay.<region>.<content-domain>``
names the relay host itself (dev convenience -- production relays get names
under the ops-managed infra zone instead). Records are gray-cloud (DNS only,
``proxied=false``): the relay does SNI passthrough, so Cloudflare must not
terminate TLS in front of it.
"""

from typing import Any
from typing import Final

import httpx

from imbue.imbue_common.pure import pure
from imbue.share_relay.data_types import RelayConfiguration
from imbue.share_relay.errors import ShareRelayError

_CF_BASE_URL: Final[str] = "https://api.cloudflare.com/client/v4"
_DNS_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0


class RelayDnsError(ShareRelayError):
    """Raised when a Cloudflare DNS record for the relay cannot be created or updated."""


@pure
def relay_dns_record_names(config: RelayConfiguration) -> list[str]:
    """The record names one relay needs: its own host name and the region's content wildcard."""
    return [f"relay.{config.region_domain}", f"*.{config.region_domain}"]


def _check_cf(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    if response.status_code >= 400 or not body.get("success", False):
        raise RelayDnsError(f"Cloudflare DNS API error {response.status_code}: {body.get('errors')}")
    return body


def upsert_a_record(client: httpx.Client, zone_id: str, record_name: str, ip_address: str) -> str:
    """Create or update one gray-cloud A record; returns the record id."""
    listing = _check_cf(
        client.get(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records", params={"type": "A", "name": record_name})
    )
    record_body = {"type": "A", "name": record_name, "content": ip_address, "ttl": 300, "proxied": False}
    existing = listing.get("result", [])
    if existing:
        record_id = str(existing[0]["id"])
        _check_cf(client.put(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records/{record_id}", json=record_body))
        return record_id
    created = _check_cf(client.post(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records", json=record_body))
    return str(created["result"]["id"])


def upsert_relay_dns_records(api_token: str, zone_id: str, config: RelayConfiguration, ip_address: str) -> list[str]:
    """Point the relay's records at ``ip_address``; returns the record ids."""
    with httpx.Client(
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=_DNS_REQUEST_TIMEOUT_SECONDS,
    ) as client:
        return [
            upsert_a_record(client, zone_id, record_name, ip_address) for record_name in relay_dns_record_names(config)
        ]
