"""End-to-end workspace stop/start against a real env (box + bucket + connector).

Leases a pool host, stops it through the connector's workspace lifecycle
(VM halt + artifact upload, then the retention finalize frees the slot),
starts it again (a restore onto a same-region box, since the finalize has
already reaped the local VM), verifies the restored coordinates answer SSH,
and releases the lease.

Needs a pool with at least one available baked slice AND workspace storage
configured for the env. An empty pool FAILS by default (the CI release flow
pre-bakes slices, so an empty pool there means the bake stage broke -- see
specs/remote-workspaces-in-ci.md); set ``MINDS_ALLOW_EMPTY_POOL=1`` to skip
instead when running against an env that legitimately has no pool (e.g.
``just minds-test-services-against dev-<you> ...``, which sets it). Missing
workspace storage still skips.

Opt-in via ``MINDS_STOP_START_RELEASE_TEST=1`` (the ``MNGR_AWS_RELEASE_TESTS``
pattern): the full cycle's measured wall time against the standing CI box was
~2.6 HOURS (the ~13GB artifact upload ran at ~1.4MB/s effective), which no CI
job budget fits -- see the spec's open questions for the follow-ups (raise the
ci tier's upload throttle or shrink the test workspace). Until one lands, the
test runs only where an operator explicitly opts in.
"""

import os
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.deployment_tests.testing import handle_no_pool_capacity
from imbue.mngr.utils.polling import poll_for_value

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

# The opt-in gate (see the module docstring); a skipif marker rather than an
# in-body skip so the verified_user fixture never provisions a real user for
# a run that is about to skip.
_STOP_START_OPT_IN = os.environ.get("MINDS_STOP_START_RELEASE_TEST") == "1"

_HTTP_TIMEOUT_SECONDS = 60.0
# The row lands on "stopped" the moment the upload verifies. Measured against
# the standing CI box, the ~13GB upload ran at ~1.4MB/s effective (~2.6h), so
# the deadline budgets that plus margin. The start downloads at ~1 GB/s and
# boots in seconds.
_STOP_DEADLINE_SECONDS = 3.5 * 3600.0
# After "stopped" the halted local VM (and the slot) is kept until the env's
# local-retention window -- which runs concurrently with the upload, from the
# stop request -- closes and the retention finalize clears the placement.
# ci/dev tiers stamp a short window via deploy.toml ([storage]
# stop_retention_seconds), so on the envs this test runs against the wait is
# minutes at most; the budget still covers the connector's 3600s default so
# the test also passes against an env without the stamp.
_SLOT_FREE_DEADLINE_SECONDS = 3600.0 + 15 * 60.0
_START_DEADLINE_SECONDS = 20 * 60.0
_POLL_INTERVAL_SECONDS = 15.0


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
    """One authenticated workspace read; records the body into ``last_out`` for failure messages."""
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
        # A start that failed lands back on stopped with the error recorded.
        if target_status == "running" and body["status"] == "stopped":
            raise AssertionError(f"start failed: {body.get('transition_error')}")
        return None

    reached, _poll_count, _elapsed = poll_for_value(
        read_workspace_if_target, timeout=deadline_seconds, poll_interval=_POLL_INTERVAL_SECONDS
    )
    assert reached is not None, f"workspace never reached {target_status} within {deadline_seconds:.0f}s; last: {last}"
    return reached


def _poll_workspace_until_slot_freed(
    client: httpx.Client,
    connector_url: str,
    user: VerifiedUserHandle,
    host_db_id: str,
) -> dict:
    """Poll a stopped workspace until the retention finalize clears its placement."""
    last: dict = {}

    def read_workspace_if_freed() -> dict | None:
        body = _read_workspace(client, connector_url, user, host_db_id, last)
        assert body["status"] == "stopped", f"workspace left stopped while awaiting finalize: {body['status']}"
        return body if body["vps_address"] is None else None

    freed, _poll_count, _elapsed = poll_for_value(
        read_workspace_if_freed, timeout=_SLOT_FREE_DEADLINE_SECONDS, poll_interval=_POLL_INTERVAL_SECONDS
    )
    assert freed is not None, f"slot was never freed within {_SLOT_FREE_DEADLINE_SECONDS:.0f}s; last: {last}"
    return freed


def _assert_ssh_banner(address: str, port: int) -> None:
    with socket.create_connection((address, port), timeout=10) as sock:
        banner = sock.recv(4)
    assert banner.startswith(b"SSH"), f"{address}:{port} did not answer with an SSH banner: {banner!r}"


# The stop deadline (3.5h) plus the slot-free (1.25h) and start (20min)
# deadlines plus lease/SSH/poll overhead; the test is opt-in (see above), so
# this long budget never holds up a default run.
@pytest.mark.timeout(6 * 3600)
@pytest.mark.skipif(
    not _STOP_START_OPT_IN,
    reason=(
        "stop/start's full cycle measured ~2.6h against the standing CI box (upload-bound), which no CI "
        "job budget fits; set MINDS_STOP_START_RELEASE_TEST=1 to run it (see the module docstring)"
    ),
)
def test_workspace_stop_uploads_frees_slot_and_start_restores(
    shared_env: Callable[[str], SharedEnvHandle], verified_user: VerifiedUserHandle
) -> None:
    env = shared_env("default")
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
            handle_no_pool_capacity("pool has no available baked slice")
        assert lease.status_code == 200, f"lease failed: {lease.status_code} {lease.text[:400]}"
        host_db_id = lease.json()["host_db_id"]
        try:
            stop = client.post(f"{connector_url}/workspaces/{host_db_id}/stop", headers=_auth_header(verified_user))
            if stop.status_code == 503:
                pytest.skip("workspace storage is not configured for this env")
            assert stop.status_code in (200, 202), f"stop refused: {stop.status_code} {stop.text[:400]}"

            # "stopped" lands the moment the upload verifies; the halted local
            # VM (and the slot) is kept through the retention window.
            _poll_workspace_until(client, connector_url, verified_user, host_db_id, "stopped", _STOP_DEADLINE_SECONDS)
            # Wait out the retention finalize too: once it clears the
            # placement, the slot is freed, the VM exists only as encrypted
            # objects in the tier bucket, and the start below always
            # exercises the restore path.
            freed = _poll_workspace_until_slot_freed(client, connector_url, verified_user, host_db_id)
            assert freed["ssh_port"] is None
            assert freed["container_ssh_port"] is None

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
