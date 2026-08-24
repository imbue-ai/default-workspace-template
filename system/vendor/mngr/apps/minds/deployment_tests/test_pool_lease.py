"""Lease / user-isolation / release against a real pre-baked pool slice.

The slice-era version of the pool test deferred by
specs/minds-deployment-tests.md, enabled by the CI pool pre-bake stage
(specs/remote-workspaces-in-ci.md): lease a pool host as user A, assert user B
cannot see (or release) it through the user-facing API, then release it as A
and assert the slice is gone. Consumes one baked slice (release destroys the
slice VM and deletes the row).
"""

from collections.abc import Callable

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.deployment_tests.testing import handle_no_pool_capacity
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS = 60.0
# A throwaway test key: leasing injects it into the slice, but this test never
# opens an SSH session -- the fast-path create test covers actual connectivity.
_TEST_SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholderTestKeyForPoolLeaseTest"


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


@pytest.mark.timeout(600)
def test_lease_is_isolated_per_user_and_release_frees_the_slice(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
    second_verified_user: VerifiedUserHandle,
) -> None:
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        lease = client.post(
            f"{connector_url}/hosts/lease",
            headers=_auth_header(verified_user),
            json={
                "ssh_public_key": _TEST_SSH_PUBLIC_KEY,
                "host_name": f"lease-isolation-{get_short_random_string()}",
                "attributes": {},
            },
        )
        if lease.status_code == 503:
            handle_no_pool_capacity("pool has no available baked slice")
        assert lease.status_code == 200, f"lease failed: {lease.status_code} {lease.text[:400]}"
        lease_body = lease.json()
        host_db_id = lease_body["host_db_id"]
        try:
            # The lease hands user A real coordinates for the slice.
            assert lease_body["vps_address"], f"lease returned no address: {lease_body}"
            assert lease_body["ssh_port"], f"lease returned no ssh port: {lease_body}"

            # User A sees the lease in their own listing; user B does not.
            owner_listing = client.get(f"{connector_url}/hosts", headers=_auth_header(verified_user))
            assert owner_listing.status_code == 200
            assert any(entry["host_db_id"] == host_db_id for entry in owner_listing.json()), (
                f"owner's /hosts listing does not include the fresh lease: {owner_listing.json()}"
            )
            other_listing = client.get(f"{connector_url}/hosts", headers=_auth_header(second_verified_user))
            assert other_listing.status_code == 200
            assert not any(entry["host_db_id"] == host_db_id for entry in other_listing.json()), (
                "another user's /hosts listing exposes user A's lease"
            )

            # User B cannot release user A's lease, and the attempt changes nothing.
            foreign_release = client.post(
                f"{connector_url}/hosts/{host_db_id}/release", headers=_auth_header(second_verified_user)
            )
            assert foreign_release.status_code in (403, 404), (
                f"another user's release attempt was not refused: {foreign_release.status_code} "
                f"{foreign_release.text[:300]}"
            )
            still_owned = client.get(f"{connector_url}/hosts", headers=_auth_header(verified_user))
            assert still_owned.status_code == 200
            assert any(entry["host_db_id"] == host_db_id for entry in still_owned.json()), (
                "user A's lease disappeared after another user's refused release attempt"
            )
        finally:
            release = client.post(f"{connector_url}/hosts/{host_db_id}/release", headers=_auth_header(verified_user))
            assert release.status_code == 200, f"release failed: {release.status_code} {release.text[:300]}"

        # Release is terminal: the row is gone from the owner's listing too.
        after_release = client.get(f"{connector_url}/hosts", headers=_auth_header(verified_user))
        assert after_release.status_code == 200
        assert not any(entry["host_db_id"] == host_db_id for entry in after_release.json()), (
            "released lease still shows in the owner's /hosts listing"
        )
