"""End-to-end check of the destroyed-workspace backup reaper against a real ci env.

Proves the full retention cycle on the deployed connector: a tombstoned
workspace record and its (reserved-name) backup bucket are reaped by the
admin trigger -- bucket emptied and deleted, record gone -- using the
admin-only window override so a fresh tombstone reaps immediately. The
30-day policy itself is served by the public policy endpoint.
"""

import uuid
from collections.abc import Callable
from typing import Final

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import ci_admin_auth_header

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
# The reap sweep does real R2 work through the Cloudflare API (list, empty,
# delete buckets) and can far exceed a normal request on a cold container.
_SWEEP_TIMEOUT_SECONDS: Final[float] = 300.0


def _connector_url(env: SharedEnvHandle) -> str:
    return str(env.urls.connector_url).rstrip("/")


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


def _post_sweep(base: str, *, is_dry_run: bool = False) -> httpx.Response:
    """POST the admin zero-window reap sweep, following Modal's long-request 303 redirects."""
    dry_run_param = "dry_run=1&" if is_dry_run else ""
    return httpx.post(
        f"{base}/admin/sweep/backup-retention?{dry_run_param}window_seconds=0",
        headers=ci_admin_auth_header(),
        timeout=_SWEEP_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


@pytest.mark.timeout(180)
def test_backup_retention_policy_endpoint_serves_the_window(shared_env: Callable[[str], SharedEnvHandle]) -> None:
    response = httpx.get(
        f"{_connector_url(shared_env('default'))}/policies/destroyed-workspace-backups", timeout=_HTTP_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    assert response.json()["retention_seconds"] == 60.0 * 60.0 * 24.0 * 30.0


@pytest.mark.timeout(600)
def test_reap_cycle_deletes_tombstoned_bucket_and_record(
    shared_env: Callable[[str], SharedEnvHandle], verified_user: VerifiedUserHandle
) -> None:
    """Tombstone -> admin trigger (window override) -> bucket + record gone."""
    base = _connector_url(shared_env("default"))
    host_id = f"host-{uuid.uuid4().hex}"
    record = {
        "host_id": host_id,
        "agent_id": f"agent-{uuid.uuid4().hex}",
        "display_name": "reap-cycle-test",
        "provider_kind": "imbue_cloud",
        "state": "active",
        "encrypted_secrets": None,
        "revision": 1,
    }

    # An ACTIVE record first: it authorizes the reserved host- bucket name and
    # proves the destroy interlock (a live workspace's backups cannot die).
    push = httpx.put(
        f"{base}/sync/records/{host_id}",
        json=record,
        headers=_auth_header(verified_user),
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    push.raise_for_status()
    create = httpx.post(
        f"{base}/buckets",
        json={"name": host_id, "access": "readwrite"},
        headers=_auth_header(verified_user),
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    create.raise_for_status()
    bucket_short_name = host_id

    try:
        interlocked = httpx.delete(
            f"{base}/buckets/{bucket_short_name}",
            headers=_auth_header(verified_user),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        assert interlocked.status_code == 409, interlocked.text

        # Tombstone the record (the server stamps destroyed_at) and reap with
        # the admin-only zero window so the fresh tombstone qualifies now.
        tombstone = httpx.put(
            f"{base}/sync/records/{host_id}",
            json={**record, "state": "destroyed", "revision": 2},
            headers=_auth_header(verified_user),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        tombstone.raise_for_status()

        dry = _post_sweep(base, is_dry_run=True)
        dry.raise_for_status()
        dry_hosts = [candidate.get("host_id") for candidate in dry.json()["result"]["candidates"]]
        assert host_id in dry_hosts, dry.text

        reap = _post_sweep(base)
        reap.raise_for_status()
        assert reap.json()["result"]["records_reaped"] >= 1, reap.text

        gone = httpx.get(
            f"{base}/buckets/{bucket_short_name}",
            headers=_auth_header(verified_user),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        assert gone.status_code == 404, gone.text
        listing = httpx.get(f"{base}/sync/records", headers=_auth_header(verified_user), timeout=_HTTP_TIMEOUT_SECONDS)
        listing.raise_for_status()
        remaining_hosts = [entry.get("host_id") for entry in listing.json().get("records", [])]
        assert host_id not in remaining_hosts
    finally:
        # Belt-and-braces cleanup for failure paths: tombstone + zero-window
        # reap is idempotent and removes anything the assertions left behind.
        httpx.put(
            f"{base}/sync/records/{host_id}",
            json={**record, "state": "destroyed", "revision": 3},
            headers=_auth_header(verified_user),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        _post_sweep(base)
