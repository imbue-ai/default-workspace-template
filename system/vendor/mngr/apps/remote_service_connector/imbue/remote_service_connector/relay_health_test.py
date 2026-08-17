"""Tests for the relay health sweep: transition rule, DNS answer sets, reconciliation."""

import json
from typing import Any

import httpx
import pytest

from imbue.remote_service_connector.relay_health import CloudflareDnsRecordSetOps
from imbue.remote_service_connector.relay_health import RELAY_DNS_TTL_SECONDS
from imbue.remote_service_connector.relay_health import apply_probe_result
from imbue.remote_service_connector.relay_health import desired_region_ips
from imbue.remote_service_connector.relay_health import reconcile_a_record_set
from imbue.remote_service_connector.relay_health import region_dns_record_names
from imbue.remote_service_connector.relay_health import run_relay_health_sweep
from imbue.remote_service_connector.relays import RELAY_HEALTHY
from imbue.remote_service_connector.relays import RELAY_UNHEALTHY
from imbue.remote_service_connector.testing import make_relay_row


class FakeDnsRecordSetOps:
    """In-memory DnsRecordSetOps: name -> {ip: record_id}."""

    def __init__(self) -> None:
        self.records_by_name: dict[str, dict[str, str]] = {}
        self._next_id = 0

    def list_a_records(self, record_name: str) -> dict[str, str]:
        return dict(self.records_by_name.get(record_name, {}))

    def create_a_record(self, record_name: str, ip_address: str) -> None:
        self._next_id += 1
        self.records_by_name.setdefault(record_name, {})[ip_address] = f"rec-{self._next_id}"

    def delete_record(self, record_id: str) -> None:
        for ip_by_name in self.records_by_name.values():
            for ip_address, existing_id in list(ip_by_name.items()):
                if existing_id == record_id:
                    del ip_by_name[ip_address]

    def ips_for(self, record_name: str) -> set[str]:
        return set(self.records_by_name.get(record_name, {}))


class FakeRelayStore:
    """In-memory RelayStore holding plain relay row dicts."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.health_updates: list[tuple[str, str, int]] = []

    def list_relays(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def upsert_relay(
        self, relay_id: str, region: str, tunnel_endpoint: str, ip_address: str, instance_name: str
    ) -> None:
        raise NotImplementedError

    def retire_relay(self, relay_id: str) -> bool:
        raise NotImplementedError

    def update_relay_health(self, relay_id: str, health: str, consecutive_probe_failures: int) -> None:
        self.health_updates.append((relay_id, health, consecutive_probe_failures))
        for row in self.rows:
            if row["relay_id"] == relay_id:
                row["health"] = health
                row["consecutive_probe_failures"] = consecutive_probe_failures

    def record_relay_login(self, host_id: str, user_label: str, relay_id: str) -> None:
        raise NotImplementedError

    def list_share_relay_logins(self, host_id: str, user_label: str) -> list[dict[str, Any]]:
        raise NotImplementedError


def test_apply_probe_result_needs_two_strikes_down_and_one_up() -> None:
    # First failure: still healthy, one strike recorded.
    assert apply_probe_result(RELAY_HEALTHY, 0, False) == (RELAY_HEALTHY, 1)
    # Second consecutive failure: unhealthy.
    assert apply_probe_result(RELAY_HEALTHY, 1, False) == (RELAY_UNHEALTHY, 2)
    assert apply_probe_result(RELAY_UNHEALTHY, 2, False) == (RELAY_UNHEALTHY, 3)
    # One success restores from anywhere.
    assert apply_probe_result(RELAY_UNHEALTHY, 5, True) == (RELAY_HEALTHY, 0)
    assert apply_probe_result(RELAY_HEALTHY, 1, True) == (RELAY_HEALTHY, 0)


def test_desired_region_ips_prefers_healthy_but_floors_at_the_active_set() -> None:
    rows = [
        make_relay_row("relay-" + "a" * 8, ip_address="203.0.113.1"),
        make_relay_row("relay-" + "b" * 8, ip_address="203.0.113.2", health=RELAY_UNHEALTHY),
        make_relay_row("relay-" + "c" * 8, ip_address="203.0.113.3", is_active=False),
    ]
    assert desired_region_ips(rows) == ["203.0.113.1"]
    # With NO healthy relay, the whole active set stays (fail visibly at
    # connect time rather than emptying the DNS answer).
    for row in rows:
        row["health"] = RELAY_UNHEALTHY
    assert desired_region_ips(rows) == ["203.0.113.1", "203.0.113.2"]


def test_region_dns_record_names_covers_wildcard_and_relay_host() -> None:
    assert region_dns_record_names("us1", "imbueminds.com") == [
        "*.us1.imbueminds.com",
        "relay.us1.imbueminds.com",
    ]


def test_reconcile_a_record_set_adds_and_removes_to_match() -> None:
    dns = FakeDnsRecordSetOps()
    dns.create_a_record("*.us1.x.com", "203.0.113.1")
    dns.create_a_record("*.us1.x.com", "203.0.113.9")

    changed = reconcile_a_record_set(dns, "*.us1.x.com", ["203.0.113.1", "203.0.113.2"])

    assert changed is True
    assert dns.ips_for("*.us1.x.com") == {"203.0.113.1", "203.0.113.2"}
    # A second pass with the same desired set is a no-op.
    assert reconcile_a_record_set(dns, "*.us1.x.com", ["203.0.113.1", "203.0.113.2"]) is False


def test_reconcile_a_record_set_refuses_an_empty_ip_set() -> None:
    # Emptying a region's record set would guarantee resolution failure for
    # every share in it; an empty desired set is a caller bug and must leave
    # the existing records untouched.
    dns = FakeDnsRecordSetOps()
    dns.create_a_record("*.us1.x.com", "203.0.113.1")

    assert reconcile_a_record_set(dns, "*.us1.x.com", []) is False
    assert dns.ips_for("*.us1.x.com") == {"203.0.113.1"}


def test_cloudflare_dns_record_set_ops_speak_the_cloudflare_wire_contract() -> None:
    # Pins the production DnsRecordSetOps' request shapes (the sweep tests run
    # against the in-memory fake). proxied=False is load-bearing: the relays do
    # SNI passthrough, so an orange-clouded record would break every share.
    seen: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.params["type"] == "A"
            assert request.url.params["name"] == "*.us1.x.com"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {"id": "rec-1", "content": "203.0.113.1"},
                        {"id": "rec-2", "content": "203.0.113.2"},
                    ],
                },
            )
        if request.method == "POST":
            assert json.loads(request.content) == {
                "type": "A",
                "name": "*.us1.x.com",
                "content": "203.0.113.3",
                "ttl": RELAY_DNS_TTL_SECONDS,
                "proxied": False,
            }
            return httpx.Response(200, json={"success": True, "result": {"id": "rec-3"}})
        return httpx.Response(200, json={"success": True, "result": {"id": "rec-1"}})

    client = httpx.Client(transport=httpx.MockTransport(_handler), base_url="https://api.cloudflare.example")
    dns_ops = CloudflareDnsRecordSetOps(zone_id="zone-1", client=client)

    assert dns_ops.list_a_records("*.us1.x.com") == {"203.0.113.1": "rec-1", "203.0.113.2": "rec-2"}
    dns_ops.create_a_record("*.us1.x.com", "203.0.113.3")
    dns_ops.delete_record("rec-1")

    assert seen == [
        ("GET", "/zones/zone-1/dns_records"),
        ("POST", "/zones/zone-1/dns_records"),
        ("DELETE", "/zones/zone-1/dns_records/rec-1"),
    ]


def test_run_relay_health_sweep_transitions_and_reconciles_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARE_CONTENT_DOMAIN", "x.com")
    rows = [
        make_relay_row(
            "relay-" + "a" * 8, ip_address="203.0.113.1", health=RELAY_HEALTHY, consecutive_probe_failures=1
        ),
        make_relay_row("relay-" + "b" * 8, ip_address="203.0.113.2"),
    ]
    store = FakeRelayStore(rows)
    dns = FakeDnsRecordSetOps()

    # Relay a's probe fails (second strike -> unhealthy); relay b's succeeds.
    counters = run_relay_health_sweep(store, lambda: dns, lambda ip_address: ip_address == "203.0.113.2")

    assert counters["probed"] == 2
    assert counters["transitions"] == 1
    assert ("relay-" + "a" * 8, RELAY_UNHEALTHY, 2) in store.health_updates
    # DNS reflects THIS sweep's health (not last minute's): only b's IP.
    assert dns.ips_for("*.us1.x.com") == {"203.0.113.2"}
    assert dns.ips_for("relay.us1.x.com") == {"203.0.113.2"}


def test_run_relay_health_sweep_is_a_noop_with_no_active_relays() -> None:
    # No SHARE_CONTENT_DOMAIN set: the early return must come before config.
    store = FakeRelayStore([make_relay_row("relay-" + "a" * 8, is_active=False)])

    def _must_not_build_dns_ops() -> FakeDnsRecordSetOps:
        # The DNS ops factory requires the sharing/Cloudflare config in
        # production, so the no-relay early return must never invoke it.
        raise AssertionError("dns ops must not be constructed when no relay is active")

    counters = run_relay_health_sweep(store, _must_not_build_dns_ops, lambda ip_address: True)

    assert counters == {"probed": 0, "transitions": 0, "dns_record_sets_changed": 0}
