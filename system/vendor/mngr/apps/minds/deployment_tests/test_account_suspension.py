"""End-to-end account suspension against a real ci env.

Proves the operator flow on the deployed stack: suspend blocks sign-in and
kills the held session's state-modifying access within one request (D3's
check-database verification), the admin view reports the suspension, and
unsuspend restores sign-in. The credential fan-out details (LiteLLM key
blocking, R2 token flips, share suspension, workspace force-stop) are
unit-tested in the connector; the tunnel-kill path additionally gets a manual
staging verification (it needs a live relay + shared workspace).
"""

from collections.abc import Callable

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import ci_admin_auth_header
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS = 60.0


def _connector_url(env: SharedEnvHandle) -> str:
    return str(env.urls.connector_url).rstrip("/")


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


@pytest.mark.timeout(300)
def test_suspend_locks_the_account_out_and_unsuspend_restores_it(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
) -> None:
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = _connector_url(env)
    admin_headers = ci_admin_auth_header()
    suspend_url = f"{connector_url}/admin/accounts/{verified_user.email}/suspend"
    unsuspend_url = f"{connector_url}/admin/accounts/{verified_user.email}/unsuspend"

    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        is_suspension_lifted = False
        try:
            # Sanity: the fixture session works before suspension.
            before = client.get(f"{connector_url}/account", headers=_auth_header(verified_user))
            assert before.status_code == 200, f"pre-suspension GET /account failed: {before.text[:400]!r}"

            suspend = client.post(
                suspend_url,
                headers=admin_headers,
                json={"reason": "deployment-test suspension"},
            )
            assert suspend.status_code == 200, f"suspend failed: {suspend.text[:400]!r}"
            report = suspend.json()
            assert report["status"] == "ok", f"suspend fan-out was partial: {report!r}"
            assert report["steps"]["sessions"]["status"] == "ok"

            # The held (revoked, still-unexpired) token is refused on a
            # state-modifying route within one request -- D3's per-request
            # core verification.
            state_change = client.post(
                f"{connector_url}/account/plan",
                headers=_auth_header(verified_user),
                json={"plan": "explorer"},
            )
            assert state_change.status_code == 401, (
                f"revoked session still accepted on a state-modifying route: "
                f"{state_change.status_code} {state_change.text[:400]!r}"
            )

            # Sign-in is blocked with the structured suspended status.
            blocked_signin = client.post(
                f"{connector_url}/auth/signin",
                json={"email": str(verified_user.email), "password": verified_user.password.get_secret_value()},
            )
            assert blocked_signin.status_code == 200
            blocked_body = blocked_signin.json()
            assert blocked_body["status"] == "ACCOUNT_SUSPENDED", f"unexpected signin result: {blocked_body!r}"
            assert "support@imbue.com" in (blocked_body.get("message") or "")

            # The operator view reports the suspension.
            shown = client.get(f"{connector_url}/admin/accounts/{verified_user.email}", headers=admin_headers)
            assert shown.status_code == 200
            shown_body = shown.json()
            assert shown_body["suspended_at"] is not None
            assert shown_body["suspended_reason"] == "deployment-test suspension"

            # The real lift: this is the call whose fan-out restores the
            # account, so it is the one under assertion.
            unsuspend = client.post(unsuspend_url, headers=admin_headers)
            assert unsuspend.status_code == 200, f"unsuspend failed: {unsuspend.text[:400]!r}"
            assert unsuspend.json()["status"] == "ok"
            is_suspension_lifted = True

            # Sign-in works again and the fresh session reaches the API.
            restored_signin = client.post(
                f"{connector_url}/auth/signin",
                json={"email": str(verified_user.email), "password": verified_user.password.get_secret_value()},
            )
            assert restored_signin.status_code == 200, f"post-unsuspend signin failed: {restored_signin.text[:400]!r}"
            restored_body = restored_signin.json()
            assert restored_body["status"] == "OK", f"post-unsuspend signin failed: {restored_body!r}"
            fresh_token = restored_body["tokens"]["access_token"]
            after = client.get(f"{connector_url}/account", headers={"Authorization": f"Bearer {fresh_token}"})
            assert after.status_code == 200, f"post-unsuspend GET /account failed: {after.text[:400]!r}"
        finally:
            # Safety net for a failed run: never strand the fixture user
            # suspended (teardown deletes it either way). Skipped once the
            # asserted unsuspend above has already lifted it.
            if not is_suspension_lifted:
                client.post(unsuspend_url, headers=admin_headers)
