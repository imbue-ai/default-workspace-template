"""End-to-end workspace stop/start against a real env (box + bucket + connector).

Leases a pool host, stops it through the connector's workspace lifecycle
(VM halt + artifact upload + slot free), starts it again (in-place restart
or a restore onto a same-region box), verifies the restored coordinates
answer SSH, and releases the lease.

Needs a pool with at least one available baked slice AND workspace storage
configured for the env, so it *skips cleanly* on envs without that
infrastructure (the shared ci env's pool is typically empty; run against a
dev env with a baked box: ``just minds-test-services-against dev-<you>
apps/minds/deployment_tests/test_workspace_stop_start.py``). The full cycle
includes the real artifact upload, whose duration is bounded by the box's
measured upload throughput -- budget tens of minutes.
"""

import socket

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.mngr.utils.polling import poll_for_value

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS = 60.0
# The stop uploads the whole artifact (~13GB at the currently-throttled
# 6-25 MB/s => 9-36 min) concurrently with the env's local-retention window
# (default WORKSPACE_STOP_RETENTION_SECONDS is 3600s), and only reports
# "stopped" once BOTH have elapsed -- so the deadline must exceed the
# retention ceiling, not just the upload. The start downloads at ~1 GB/s
# and boots in seconds.
_STOP_DEADLINE_SECONDS = 80 * 60.0
_START_DEADLINE_SECONDS = 20 * 60.0
_POLL_INTERVAL_SECONDS = 15.0


def _connector_url(env: SharedEnvHandle) -> str:
    return str(env.urls.connector_url).rstrip("/")


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


def _poll_workspace_until(
    client: httpx.Client,
    connector_url: str,
    user: VerifiedUserHandle,
    host_db_id: str,
    target_status: str,
    deadline_seconds: float,
) -> dict:
    last: dict = {}

    def read_workspace_if_target() -> dict | None:
        response = client.get(f"{connector_url}/workspaces/{host_db_id}", headers=_auth_header(user))
        assert response.status_code == 200, f"workspace poll failed: {response.status_code} {response.text[:300]}"
        body = response.json()
        last.clear()
        last.update(body)
        if body["status"] == target_status:
            return body
        # A start that failed lands back on stopped with the error recorded.
        if target_status == "running" and body["status"] == "stopped":
            raise AssertionError(f"start failed: {body.get('transition_error')}")
        return None

    reached, _poll_count, _elapsed = poll_for_value(
        read_workspace_if_target, timeout=deadline_seconds, poll_interval=_POLL_INTERVAL_SECONDS
    )
    assert reached is not None, f"workspace never reached {target_status} within {deadline_seconds:.0f}s; last: {last}"
    return reached


def _assert_ssh_banner(address: str, port: int) -> None:
    with socket.create_connection((address, port), timeout=10) as sock:
        banner = sock.recv(4)
    assert banner.startswith(b"SSH"), f"{address}:{port} did not answer with an SSH banner: {banner!r}"


def test_workspace_stop_uploads_frees_slot_and_start_restores(
    shared_env: SharedEnvHandle, verified_user: VerifiedUserHandle
) -> None:
    env = shared_env
    wait_for_env_ready(env)
    connector_url = _connector_url(env)

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        lease = client.post(
            f"{connector_url}/hosts/lease",
            headers=_auth_header(verified_user),
            json={
                "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholderTestKeyForStopStart",
                "host_name": "stop-start-probe",
                "attributes": {},
            },
        )
        if lease.status_code == 503:
            pytest.skip("pool has no available baked slice; run against a dev env with a baked box")
        assert lease.status_code == 200, f"lease failed: {lease.status_code} {lease.text[:400]}"
        host_db_id = lease.json()["host_db_id"]
        try:
            stop = client.post(f"{connector_url}/workspaces/{host_db_id}/stop", headers=_auth_header(verified_user))
            if stop.status_code == 503:
                pytest.skip("workspace storage is not configured for this env")
            assert stop.status_code in (200, 202), f"stop refused: {stop.status_code} {stop.text[:400]}"

            stopped = _poll_workspace_until(
                client, connector_url, verified_user, host_db_id, "stopped", _STOP_DEADLINE_SECONDS
            )
            # A stopped workspace holds no placement: its slot is freed and its
            # VM exists only as encrypted objects in the tier bucket.
            assert stopped["vps_address"] is None
            assert stopped["ssh_port"] is None
            assert stopped["container_ssh_port"] is None

            start = client.post(f"{connector_url}/workspaces/{host_db_id}/start", headers=_auth_header(verified_user))
            assert start.status_code in (200, 202), f"start refused: {start.status_code} {start.text[:400]}"
            running = _poll_workspace_until(
                client, connector_url, verified_user, host_db_id, "running", _START_DEADLINE_SECONDS
            )
            assert running["vps_address"], "running workspace must have a box address"
            # Both restored endpoints answer SSH with the workspace's own keys.
            _assert_ssh_banner(running["vps_address"], int(running["ssh_port"]))
            _assert_ssh_banner(running["vps_address"], int(running["container_ssh_port"]))
        finally:
            release = client.post(f"{connector_url}/hosts/{host_db_id}/release", headers=_auth_header(verified_user))
            assert release.status_code == 200, f"release failed: {release.status_code} {release.text[:300]}"
