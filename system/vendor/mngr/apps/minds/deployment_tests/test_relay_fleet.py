"""Relay-fleet checks against a real deployed env (blueprint/multi-relay phase 1).

Proves the fleet inventory is live end to end: the connector's relays table
lists the env's relays as healthy, a share's assignment endpoint hands the
workspace every relay of its region, and -- on an env whose region runs two or
more relays (staging/production shape; dev/ci envs run one) -- stopping one
relay's frps leaves the region serviceable through the survivor and the
stopped relay recovers on restart.

The failover test exercises relay-level liveness only (each relay's healthz,
which itself probes the tunnel-control port); the health sweep's DNS reaction
and the full visitor-path check through a live shared workspace stay on the
manual staging soak checklist (they need DNS-propagation waits and a running
shared workspace plus a browser).
"""

import shlex
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import ci_admin_auth_header
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.mngr_imbue_cloud.primitives import CI_TIER
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
_CI_RELAY_SSH_VAULT_PATH: Final[VaultPath] = VaultPath("secrets/minds/ci/relay-ssh")

_RELAY_HEALTHZ_PORT: Final[int] = 8080
_RELAY_RECOVERY_TIMEOUT_SECONDS: Final[float] = 60.0
_RELAY_RECOVERY_POLL_SECONDS: Final[float] = 2.0


def _connector_url(env: SharedEnvHandle) -> str:
    return str(env.urls.connector_url).rstrip("/")


def _skip_on_per_run_ci_env(env: SharedEnvHandle) -> None:
    """Per-run ``ci-*`` envs never carry a relay fleet, so the fleet checks skip there.

    Relays are registered per region label by ``just provision-dev-relay``
    (dev/ci; the env name is the region label) or the staging/production
    runbook -- the CI orchestrator's ephemeral envs go through neither, so an
    empty fleet is their designed state, not an outage.
    """
    if tier_for_env_name(str(env.urls.env_name)) == CI_TIER:
        pytest.skip("per-run ci envs provision no relay fleet; these checks target standing envs")


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


def _list_active_relays(base: str) -> list[dict[str, Any]]:
    response = httpx.get(f"{base}/admin/relays", headers=ci_admin_auth_header(), timeout=_HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return [row for row in response.json()["relays"] if row["is_active"]]


def _probe_healthz(ip_address: str) -> bool:
    try:
        response = httpx.get(f"http://{ip_address}:{_RELAY_HEALTHZ_PORT}/healthz", timeout=10.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


@pytest.mark.timeout(180)
def test_relay_fleet_is_registered_and_healthy(shared_env: Callable[[str], SharedEnvHandle]) -> None:
    """Every active relay row answers its healthz probe directly (fleet inventory matches reality)."""
    env = shared_env("default")
    _skip_on_per_run_ci_env(env)
    relays = _list_active_relays(_connector_url(env))
    assert relays, "no active relay registered for this env (run `just provision-dev-relay`)"
    for relay in relays:
        assert _probe_healthz(str(relay["ip_address"])), f"relay {relay['relay_id']} healthz unreachable"


@pytest.mark.timeout(300)
def test_share_assignment_returns_the_regions_relay_fleet(
    shared_env: Callable[[str], SharedEnvHandle], verified_user: VerifiedUserHandle
) -> None:
    """A fresh share's relay token fetches an assignment naming every active relay of its region."""
    env = shared_env("default")
    _skip_on_per_run_ci_env(env)
    base = _connector_url(env)
    host_id = f"host-{uuid.uuid4().hex}"
    created = httpx.post(
        f"{base}/shares",
        json={"host_id": host_id},
        headers=_auth_header(verified_user),
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    created.raise_for_status()
    share = created.json()
    try:
        assignment = httpx.get(
            f"{base}/shares/assignment",
            headers={"Authorization": f"Bearer {share['relay_token']}"},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        assignment.raise_for_status()
        body = assignment.json()
        assert body["workspace_domain"] == share["workspace_domain"]
        assert body["poll_seconds"] > 0
        expected_relay_ids = sorted(
            row["relay_id"] for row in _list_active_relays(base) if row["region"] == share["region"]
        )
        assert sorted(entry["relay_id"] for entry in body["relay_endpoints"]) == expected_relay_ids
        assert body["relay_endpoints"], "assignment returned no relay endpoints"
    finally:
        httpx.delete(
            f"{base}/shares/{host_id}", headers=_auth_header(verified_user), timeout=_HTTP_TIMEOUT_SECONDS
        ).raise_for_status()


def _relay_ssh(relay_ip: str, key_path: str, command: str) -> None:
    completed = subprocess.run(
        [
            "ssh",
            "-i",
            key_path,
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=yes",
            f"debian@{relay_ip}",
            command,
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (
        f"ssh {shlex.quote(command)} on {relay_ip} failed ({completed.returncode}): {completed.stderr.decode()[:500]}"
    )


@pytest.mark.timeout(600)
def test_relay_failover_keeps_the_region_serviceable(
    shared_env: Callable[[str], SharedEnvHandle], tmp_path: Path
) -> None:
    """Stop one relay's frps: the survivor keeps serving; the stopped relay recovers on restart.

    Skips on single-relay regions (dev/ci envs) -- the multi-relay shape is a
    staging/production property.
    """
    env = shared_env("default")
    _skip_on_per_run_ci_env(env)
    base = _connector_url(env)
    relays = _list_active_relays(base)
    relays_by_region: dict[str, list[dict[str, Any]]] = {}
    for relay in relays:
        relays_by_region.setdefault(str(relay["region"]), []).append(relay)
    multi_relay_regions = {region: rows for region, rows in relays_by_region.items() if len(rows) >= 2}
    if not multi_relay_regions:
        pytest.skip("env has no multi-relay region; failover applies to the staging/production fleet shape")

    relay_ssh = read_vault_kv(_CI_RELAY_SSH_VAULT_PATH)
    key_path = tmp_path / "relay_key"
    key_path.write_text(relay_ssh["RELAY_SSH_PRIVATE_KEY"].rstrip("\n") + "\n")
    key_path.chmod(0o600)

    region_rows = next(iter(multi_relay_regions.values()))
    stopped, survivor = region_rows[0], region_rows[1]
    _relay_ssh(str(stopped["ip_address"]), str(key_path), "sudo systemctl stop frps")
    try:
        # The survivor keeps answering while the stopped relay reports down
        # (a stopped frps leaves healthz up but answering 503).
        assert _probe_healthz(str(survivor["ip_address"])), "survivor relay went unhealthy during failover"
        assert not _probe_healthz(str(stopped["ip_address"])), "stopped relay still reports healthy"
    finally:
        _relay_ssh(str(stopped["ip_address"]), str(key_path), "sudo systemctl start frps")

    # The stopped relay comes back within the recovery window (poll with a
    # deadline; the sleep paces the healthz probes).
    deadline = time.monotonic() + _RELAY_RECOVERY_TIMEOUT_SECONDS
    while not _probe_healthz(str(stopped["ip_address"])):
        assert time.monotonic() < deadline, "stopped relay did not recover after frps restart"
        time.sleep(_RELAY_RECOVERY_POLL_SECONDS)
