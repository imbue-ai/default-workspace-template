"""The Cloudflare-proxied DNS record for one tier's ingest hostname.

Exactly one orange-cloud A record per instance: ``telemetry.<tier domain>``
(OpenObserve) or ``errors.<tier domain>`` (Bugsink) -> the instance IP.
Proxied (unlike the relays' gray-cloud records, which do SNI passthrough
and must not have Cloudflare in front): the proxy is what lets the origin
firewall admit only Cloudflare's ranges, hides the origin IP, and provides the
edge TLS the senders see.
"""

from typing import Any
from typing import Final

import httpx

from imbue.observability.errors import ObservabilityError
from imbue.observability.primitives import PublicIngestHostname

_CF_BASE_URL: Final[str] = "https://api.cloudflare.com/client/v4"
_DNS_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

# TTL 1 is Cloudflare's "automatic" and the only valid value for proxied
# records.
_PROXIED_RECORD_TTL: Final[int] = 1


class TelemetryDnsError(ObservabilityError):
    """Raised when the ingest hostname's Cloudflare record cannot be reconciled."""


def _check_cf(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        # Edge proxies can answer with HTML (502/504) or an empty body; surface
        # the status rather than an opaque JSON decode error.
        raise TelemetryDnsError(
            f"Cloudflare DNS API returned non-JSON response (status {response.status_code})"
        ) from exc
    if response.status_code >= 400 or not body.get("success", False):
        raise TelemetryDnsError(f"Cloudflare DNS API error {response.status_code}: {body.get('errors')}")
    return body


def upsert_proxied_ingest_record(client: httpx.Client, zone_id: str, hostname: PublicIngestHostname, ip: str) -> bool:
    """Point the proxied A record for ``hostname`` at exactly ``ip``; returns whether anything changed.

    Instance replacement is sequential (single-writer), so the record always
    carries exactly one IP: extra A records from a partial earlier pass are
    deleted rather than kept as siblings.
    """
    listing = _check_cf(
        client.get(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records", params={"type": "A", "name": str(hostname)})
    )
    existing_records = [record for record in listing.get("result", []) if isinstance(record, dict)]
    record_body = {
        "type": "A",
        "name": str(hostname),
        "content": ip,
        "ttl": _PROXIED_RECORD_TTL,
        "proxied": True,
    }
    if not existing_records:
        _check_cf(client.post(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records", json=record_body))
        return True

    is_changed = False
    keeper = existing_records[0]
    if str(keeper.get("content")) != ip or keeper.get("proxied") is not True:
        _check_cf(client.put(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records/{keeper['id']}", json=record_body))
        is_changed = True
    for extra_record in existing_records[1:]:
        _check_cf(client.delete(f"{_CF_BASE_URL}/zones/{zone_id}/dns_records/{extra_record['id']}"))
        is_changed = True
    return is_changed


def reconcile_ingest_dns_record(api_token: str, zone_id: str, hostname: PublicIngestHostname, ip: str) -> bool:
    """Reconcile the tier's proxied ingest record with its own short-lived client."""
    with httpx.Client(
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=_DNS_REQUEST_TIMEOUT_SECONDS,
    ) as client:
        return upsert_proxied_ingest_record(client, zone_id, hostname, ip)
