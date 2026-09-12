"""End-to-end machine resize against a real env (specs/slice-fleet).

Leases a default-size machine, records a resize (units up + a disk grow)
through the connector's machine endpoint, restarts the machine through the
workspace lifecycle (stop, then start -- the start is what applies the
targets), and verifies the applied size from BOTH sides: the connector's
recorded state (current = target, targets cleared) and the machine itself
over SSH (the VM's visible RAM and the grown data filesystem).

Needs a pool with at least one available baked GEN-2 slice and workspace
storage configured for the env; a gen-1-only tier skips (the resize endpoint
answers 409 "not resizable" there). An empty pool FAILS by default (see
test_workspace_stop_start); ``MINDS_ALLOW_EMPTY_POOL=1`` downgrades that to a
skip.

Runs whenever the minds release tier runs. The cycle includes a stop upload
(the wall time is upload-bound); the in-place restart path keeps this cheaper
than the full stop/start test (no retention wait), but the upload still
dominates, hence the generous timeout.
"""

import io
import socket
from collections.abc import Callable
from typing import Any
from typing import Final

import httpx
import paramiko
import psycopg2
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import LEASE_MAX_BOX_GENERATION
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.deployment_tests.testing import handle_no_pool_capacity
from imbue.mngr.providers.ssh_utils import generate_ed25519_host_keypair
from imbue.mngr.utils.polling import poll_for_value

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS: Final = 60.0
# The stop lands the moment the upload verifies (upload-bound; see the
# stop/start release test's measurements).
_STOP_DEADLINE_SECONDS: Final = 3.5 * 3600.0
# The start restarts in place (the VM never left its origin box within the
# retention window), which applies the resize and boots in minutes.
_START_DEADLINE_SECONDS: Final = 20 * 60.0
_POLL_INTERVAL_SECONDS: Final = 15.0
_SSH_COMMAND_TIMEOUT_SECONDS: Final = 30.0

# The sizes this test drives: default (8 units / 28GB) -> 16 units + 56GB.
_TARGET_UNITS: Final = 16
_TARGET_DISK_GB: Final = 56


def _raise_machine_size_quotas(env: SharedEnvHandle, user_id: str) -> None:
    """Raise the test account's machine-size entitlements above the resize targets.

    A fresh account's plan allows exactly the default 8-unit machine, which the
    lease itself consumes, so the 16-unit resize would be a legitimate
    ``quota_exceeded`` 403. The deployment-test fixtures carry no admin API key
    (the quota-enforcement test's precedent), so the entitlements are written
    through the pool DSN. The lease's own quota check has already lazily
    created the entitlements row by the time this runs.
    """
    conn = psycopg2.connect(env.neon_host_pool_dsn.get_secret_value())
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE account_entitlements SET max_active_machine_units = %s, "
                    "max_total_machine_disk_gb = %s WHERE user_id = %s",
                    (_TARGET_UNITS * 2, _TARGET_DISK_GB * 4, user_id),
                )
                assert cur.rowcount == 1, f"no entitlements row for user {user_id!r} (lease not yet made?)"
    finally:
        conn.close()


def _connector_url(env: SharedEnvHandle) -> str:
    return str(env.urls.connector_url).rstrip("/")


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


def _read_workspace(
    client: httpx.Client,
    connector_url: str,
    user: VerifiedUserHandle,
    host_db_id: str,
    last_out: dict[str, Any],
) -> dict:
    response = client.get(f"{connector_url}/workspaces/{host_db_id}", headers=_auth_header(user))
    assert response.status_code == 200, f"workspace poll failed: {response.status_code} {response.text[:300]}"
    body = response.json()
    last_out.clear()
    last_out.update(body)
    return body


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
        body = _read_workspace(client, connector_url, user, host_db_id, last)
        if body["status"] == target_status:
            return body
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


def _run_vm_command(address: str, port: int, key: paramiko.Ed25519Key, command: str) -> str:
    """One root SSH exec against the machine's VM endpoint (the leased key is authorized there)."""
    client = paramiko.SSHClient()
    # A release probe against a machine the test just leased: TOFU is
    # acceptable here (production paths pin recorded keys).
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=address, port=port, username="root", pkey=key, timeout=30)
        _stdin, stdout, stderr = client.exec_command(command, timeout=_SSH_COMMAND_TIMEOUT_SECONDS)
        exit_status = stdout.channel.recv_exit_status()
        output = stdout.read().decode()
        assert exit_status == 0, f"VM command {command!r} failed (exit {exit_status}): {stderr.read().decode()[:300]}"
        return output
    finally:
        client.close()


@pytest.mark.timeout(5 * 3600)
def test_machine_resize_applies_units_and_disk_grow_at_restart(
    shared_env: Callable[[str], SharedEnvHandle], verified_user: VerifiedUserHandle
) -> None:
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = _connector_url(env)

    # A real keypair: the resize verification SSHes the machine's VM root to
    # read its visible RAM and the grown data filesystem.
    private_key_pem, ssh_public_key = generate_ed25519_host_keypair()
    vm_key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key_pem))

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        lease = client.post(
            f"{connector_url}/hosts/lease",
            headers=_auth_header(verified_user),
            json={
                "ssh_public_key": ssh_public_key,
                "host_name": "machine-resize-probe",
                "attributes": {},
                "max_box_generation": LEASE_MAX_BOX_GENERATION,
            },
        )
        if lease.status_code == 503:
            handle_no_pool_capacity("pool has no available baked slice")
        assert lease.status_code == 200, f"lease failed: {lease.status_code} {lease.text[:400]}"
        lease_body = lease.json()
        host_db_id = lease_body["host_db_id"]
        try:
            _raise_machine_size_quotas(env, str(verified_user.supertokens_user_id))
            # Record the resize: units up + a disk grow, applied at the next start.
            resize = client.post(
                f"{connector_url}/machines/{host_db_id}/resize",
                headers=_auth_header(verified_user),
                json={"target_memory_units": _TARGET_UNITS, "target_disk_gb": _TARGET_DISK_GB},
            )
            if resize.status_code == 409 and "not resizable" in resize.text:
                pytest.skip("this tier's pool is generation 1; machines become resizable on gen-2 boxes")
            assert resize.status_code == 200, f"resize refused: {resize.status_code} {resize.text[:400]}"
            recorded = resize.json()
            assert recorded["target_memory_units"] == _TARGET_UNITS, recorded
            assert recorded["target_disk_gb"] == _TARGET_DISK_GB, recorded

            # Restart (stop, then start): the start applies the targets --
            # in place when the origin box has room, else via a restore.
            stop = client.post(f"{connector_url}/workspaces/{host_db_id}/stop", headers=_auth_header(verified_user))
            if stop.status_code == 503:
                pytest.skip("workspace storage is not configured for this env")
            assert stop.status_code in (200, 202), f"stop refused: {stop.status_code} {stop.text[:400]}"
            _poll_workspace_until(client, connector_url, verified_user, host_db_id, "stopped", _STOP_DEADLINE_SECONDS)

            start = client.post(f"{connector_url}/workspaces/{host_db_id}/start", headers=_auth_header(verified_user))
            assert start.status_code in (200, 202), f"start refused: {start.status_code} {start.text[:400]}"
            running = _poll_workspace_until(
                client, connector_url, verified_user, host_db_id, "running", _START_DEADLINE_SECONDS
            )

            # Connector-side: the start restamped current = target and
            # cleared the pending targets.
            assert running["memory_units"] == _TARGET_UNITS, running
            assert running["disk_gb"] == _TARGET_DISK_GB, running
            assert running["target_memory_units"] is None, running
            assert running["target_disk_gb"] is None, running

            # Machine-side: the VM sees the new RAM (MemTotal is slightly
            # below the advertised size -- kernel/firmware reservations -- so
            # allow ~1GiB of slack) and the data filesystem grew to the new
            # disk (the in-guest oneshot runs at boot; df reports GB, allow
            # filesystem overhead slack).
            address = running["vps_address"]
            vm_port = int(running["ssh_port"])
            _assert_ssh_banner(address, vm_port)
            _assert_ssh_banner(address, int(running["container_ssh_port"]))
            mem_total_kib = int(
                _run_vm_command(address, vm_port, vm_key, "awk '/^MemTotal:/{print $2}' /proc/meminfo").strip()
            )
            assert mem_total_kib > (_TARGET_UNITS - 1) * 1024 * 1024, (
                f"VM MemTotal {mem_total_kib}KiB is below the resized {_TARGET_UNITS}GiB"
            )
            data_fs_gb = int(
                _run_vm_command(
                    address, vm_port, vm_key, "df -BG --output=size /mnt/mngr-data | tail -1 | tr -dc '0-9'"
                ).strip()
            )
            assert data_fs_gb >= _TARGET_DISK_GB - 4, (
                f"data filesystem is {data_fs_gb}GB, below the grown {_TARGET_DISK_GB}GB target"
            )
        finally:
            release = client.post(f"{connector_url}/hosts/{host_db_id}/release", headers=_auth_header(verified_user))
            assert release.status_code == 200, f"release failed: {release.status_code} {release.text[:300]}"
