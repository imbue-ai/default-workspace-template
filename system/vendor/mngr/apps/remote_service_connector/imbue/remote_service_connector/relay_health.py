"""Relay health sweep: probe every relay's /healthz and keep the region DNS record sets in step.

Runs as the `relay_health_sweep` cron (every minute). Health only steers
*visitors*: an unhealthy relay is pulled from its region's DNS answers (so new
resolutions avoid it) but keeps receiving workspace tunnels, so it serves
again the moment it recovers. Transitions are logged at error level -- that is
the alerting hook for now (log-based alerting is wired separately).
"""

import functools
import logging
from collections.abc import Callable
from typing import Any
from typing import Final
from typing import Protocol

import httpx
from pydantic import BaseModel
from pydantic import ConfigDict

from imbue.modal_app_kit.metrics import emit_metric
from imbue.remote_service_connector.cloudflare import CF_BASE_URL
from imbue.remote_service_connector.cloudflare import cf_check
from imbue.remote_service_connector.relays import RELAY_HEALTHY
from imbue.remote_service_connector.relays import RELAY_UNHEALTHY
from imbue.remote_service_connector.relays import RelayStore
from imbue.remote_service_connector.shares import require_share_env
from imbue.remote_service_connector.shares import share_content_domain

logger = logging.getLogger(__name__)

# The relay's healthcheck HTTP port (serve_healthcheck in apps/share_relay).
RELAY_HEALTHZ_PORT: Final[int] = 8080

_HEALTHZ_TIMEOUT_SECONDS: Final[float] = 5.0

# Strikes before a relay is marked unhealthy (and pulled from DNS): two
# consecutive failed probes, so a single probe blip never flaps DNS. One
# healthy probe restores immediately.
_FAILURES_BEFORE_UNHEALTHY: Final[int] = 2

# TTL for the region wildcard / relay A records, low so a health-driven pull
# propagates quickly (browsers' connect-failure fallback covers the window).
RELAY_DNS_TTL_SECONDS: Final[int] = 60


def probe_relay_healthz(ip_address: str) -> bool:
    """One liveness probe: GET /healthz on the relay; any non-200 or transport error is a failure."""
    try:
        response = httpx.get(f"http://{ip_address}:{RELAY_HEALTHZ_PORT}/healthz", timeout=_HEALTHZ_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.debug("Relay healthz probe to %s failed: %s", ip_address, exc)
        return False
    return response.status_code == 200


def apply_probe_result(health: str, consecutive_failures: int, is_probe_ok: bool) -> tuple[str, int]:
    """The health transition rule: 2 consecutive failures mark unhealthy, 1 success restores."""
    if is_probe_ok:
        return (RELAY_HEALTHY, 0)
    failures = consecutive_failures + 1
    return (RELAY_UNHEALTHY if failures >= _FAILURES_BEFORE_UNHEALTHY else health, failures)


def desired_region_ips(region_relay_rows: list[dict[str, Any]]) -> list[str]:
    """The DNS answer set for one region: healthy active relay IPs, floored at the full active set.

    The floor: when NO relay in the region is healthy, pulling everything would
    turn a (possibly probe-side) outage into guaranteed NXDOMAIN-like death --
    keep every active IP instead and fail visibly at connect time.
    """
    active_rows = [row for row in region_relay_rows if row["is_active"]]
    healthy_ips = sorted({str(row["ip_address"]) for row in active_rows if row["health"] == RELAY_HEALTHY})
    if healthy_ips:
        return healthy_ips
    return sorted({str(row["ip_address"]) for row in active_rows})


def region_dns_record_names(region: str, content_domain: str) -> list[str]:
    """The record names one region needs: the content wildcard and the relay host name."""
    return [f"*.{region}.{content_domain}", f"relay.{region}.{content_domain}"]


class DnsRecordSetOps(Protocol):
    """The A-record-set operations the sweep needs, so tests can fake Cloudflare."""

    def list_a_records(self, record_name: str) -> dict[str, str]:
        """Existing A records for the name: ip -> record id."""
        ...

    def create_a_record(self, record_name: str, ip_address: str) -> None: ...
    def delete_record(self, record_id: str) -> None: ...


class CloudflareDnsRecordSetOps(BaseModel):
    """DnsRecordSetOps against the content domain's Cloudflare zone (gray-cloud records)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    zone_id: str
    client: httpx.Client

    def list_a_records(self, record_name: str) -> dict[str, str]:
        response = self.client.get(f"/zones/{self.zone_id}/dns_records", params={"type": "A", "name": record_name})
        result = cf_check(response).get("result", [])
        return {str(record["content"]): str(record["id"]) for record in result}

    def create_a_record(self, record_name: str, ip_address: str) -> None:
        response = self.client.post(
            f"/zones/{self.zone_id}/dns_records",
            json={
                "type": "A",
                "name": record_name,
                "content": ip_address,
                "ttl": RELAY_DNS_TTL_SECONDS,
                "proxied": False,
            },
        )
        cf_check(response)

    def delete_record(self, record_id: str) -> None:
        response = self.client.delete(f"/zones/{self.zone_id}/dns_records/{record_id}")
        cf_check(response)


@functools.cache
def get_dns_record_set_ops() -> DnsRecordSetOps:
    client = httpx.Client(
        base_url=CF_BASE_URL,
        headers={"Authorization": f"Bearer {require_share_env('CLOUDFLARE_API_TOKEN')}"},
        timeout=30.0,
    )
    return CloudflareDnsRecordSetOps(zone_id=require_share_env("CLOUDFLARE_ZONE_ID"), client=client)


def reconcile_a_record_set(dns_ops: DnsRecordSetOps, record_name: str, desired_ips: list[str]) -> bool:
    """Make the name's A records exactly ``desired_ips``; returns whether anything changed.

    Refuses an empty desired set: emptying a region's record set would take
    every share in it down even harder than a dead relay (guaranteed
    resolution failure). ``desired_region_ips`` floors at the active set, so
    an empty set here means a caller bug -- log loudly and leave DNS alone.
    """
    if not desired_ips:
        logger.error("Refusing to reconcile %s to an empty IP set", record_name)
        return False
    existing_id_by_ip = dns_ops.list_a_records(record_name)
    desired = set(desired_ips)
    is_changed = False
    for ip_address in sorted(desired - set(existing_id_by_ip)):
        dns_ops.create_a_record(record_name, ip_address)
        is_changed = True
    for stale_ip in sorted(set(existing_id_by_ip) - desired):
        dns_ops.delete_record(existing_id_by_ip[stale_ip])
        is_changed = True
    return is_changed


def run_relay_health_sweep(
    store: RelayStore,
    # A factory (not an instance) so the sharing/Cloudflare config is only
    # required once the no-active-relay early return has passed; production
    # passes ``get_dns_record_set_ops``.
    make_dns_ops: Callable[[], DnsRecordSetOps],
    # Injected so tests can drive probe outcomes; production passes
    # ``probe_relay_healthz``.
    probe: Callable[[str], bool],
) -> dict[str, int]:
    """One sweep pass: probe every active relay, apply transitions, reconcile each region's DNS."""
    relay_rows = store.list_relays()
    counters = {"probed": 0, "transitions": 0, "dns_record_sets_changed": 0}
    # A tier with no registered relays has nothing to probe or reconcile;
    # return before touching the sharing config so the cron is a no-op there.
    if not any(row["is_active"] for row in relay_rows):
        return counters
    content_domain = share_content_domain()
    dns_ops = make_dns_ops()

    # Probe + transition. The updated health is folded back into the local rows
    # so the DNS pass below sees this sweep's results, not last minute's.
    for row in relay_rows:
        if not row["is_active"]:
            continue
        counters["probed"] += 1
        is_probe_ok = probe(str(row["ip_address"]))
        new_health, new_failures = apply_probe_result(
            str(row["health"]), int(row["consecutive_probe_failures"]), is_probe_ok
        )
        if new_health != row["health"]:
            counters["transitions"] += 1
            emit_metric("relay_health_transition", 1, {"region": str(row["region"]), "to": new_health})
            # Error level on purpose: a relay health transition is the
            # alerting signal for relay outages, reported to the tier's
            # error tracker at top priority.
            logger.error(
                "Relay %s (%s, %s) transitioned %s -> %s",
                row["relay_id"],
                row["region"],
                row["ip_address"],
                row["health"],
                new_health,
            )
        if new_health != row["health"] or new_failures != row["consecutive_probe_failures"]:
            store.update_relay_health(str(row["relay_id"]), new_health, new_failures)
        row["health"] = new_health
        row["consecutive_probe_failures"] = new_failures

    # Reconcile each region's record set against the (possibly updated) health.
    regions = sorted({str(row["region"]) for row in relay_rows if row["is_active"]})
    for region in regions:
        region_rows = [row for row in relay_rows if str(row["region"]) == region]
        desired_ips = desired_region_ips(region_rows)
        for record_name in region_dns_record_names(region, content_domain):
            if reconcile_a_record_set(dns_ops, record_name, desired_ips):
                counters["dns_record_sets_changed"] += 1
                emit_metric("relay_dns_record_set_changed", 1, {"record_name": record_name})
                # Warning level on purpose, even for benign changes (e.g. a
                # new relay's IP joining the set): any DNS answer change is
                # part of the alerting signal, but a change is not itself an
                # outage -- the health transition above carries error level.
                logger.warning("Reconciled DNS record set for %s to %s", record_name, desired_ips)
    return counters
