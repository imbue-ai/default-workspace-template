"""``minds_services`` tests: the hosted minds web chrome (``/web``) on a live env.

A Playwright pass over the browser-only workspace loop that needs no pool
hardware: sign in, set the master password (first visit), render the overview
from seeded workspace records, destroy a record (tombstone; there is no lease
behind it, exactly like a workspace whose lease is already gone), and unlock
again from a fresh browser session -- wrong password first, then the right
one. Full create-through-exec deliberately stays out (it needs a baked pool
slice; see the deployment-tests README).

Skipped when the Playwright browser is not installed on the runner
(``playwright install chromium``). Signups use throwaway
``test-<hex>@example.test`` addresses so the conftest's stale-test-user sweep
deletes leftovers from crashed runs.
"""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_TEST_TIMEOUT_SECONDS = 300
_UI_TIMEOUT_MS = 30_000
_MASTER_PASSWORD = "master-pw-for-e2e-1"


def _fresh_credentials() -> tuple[str, str]:
    # Must match conftest's _STALE_TEST_USER_EMAIL_PATTERN so the sweep
    # deletes leftover accounts from crashed runs.
    return f"test-{uuid4().hex}@example.test", f"pw-{uuid4().hex}"


def _wire_record(host_id: str, display_name: str, state: str) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "agent_id": f"agent-{uuid4().hex}",
        "display_name": display_name,
        "color": None,
        "provider_kind": "imbue_cloud",
        "hosting_device_id": None,
        "device_label": "e2e-test",
        "state": state,
        "restored_from_host_id": None,
        "encrypted_secrets": None,
        "revision": 1,
    }


def _signup_in_context(context: Any, connector_url: str, email: str, password: str) -> None:
    """Establish the browser cookie session via the JSON signup API.

    ``context.request`` shares the browser context's cookie jar, so the
    session cookies land where the chrome's fetches will send them.
    """
    resp = context.request.post(
        f"{connector_url}/accounts/api/signup",
        data={"email": email, "password": password, "turnstile_token": ""},
    )
    assert resp.ok, resp.text()
    assert resp.json()["status"] == "OK", resp.text()


def _signin_in_context(context: Any, connector_url: str, email: str, password: str) -> None:
    resp = context.request.post(
        f"{connector_url}/accounts/api/signin",
        data={"email": email, "password": password, "turnstile_token": ""},
    )
    assert resp.ok, resp.text()
    assert resp.json()["status"] == "OK", resp.text()


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_web_chrome_unlock_overview_and_destroy_loop(shared_env: Callable[[str], SharedEnvHandle]) -> None:
    """Drive the chrome's poolless loop: set password -> overview tiles -> destroy -> re-unlock."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    email, password = _fresh_credentials()
    active_host_id = "host-" + uuid4().hex
    destroyed_host_id = "host-" + uuid4().hex

    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright_api.Error as exc:
            pytest.skip(f"Playwright chromium is not installed on this runner: {exc}")
        try:
            # -- First session: sign up, seed records, set the master password.
            context = browser.new_context()
            _signup_in_context(context, connector_url, email, password)
            for record in (
                _wire_record(active_host_id, "Seeded Cloud WS", "active"),
                _wire_record(destroyed_host_id, "Tombstoned WS", "destroyed"),
            ):
                seeded = context.request.put(f"{connector_url}/sync/records/{record['host_id']}", data=record)
                assert seeded.ok, seeded.text()

            page = context.new_page()
            page.goto(f"{connector_url}/web")

            # First visit: no key bundle yet, so the chrome walks us through
            # setting the master password (the setup variant shows a confirm
            # field; the unlock variant does not).
            page.wait_for_selector("text=Set a master password", timeout=_UI_TIMEOUT_MS)
            page.get_by_placeholder("Master password").fill(_MASTER_PASSWORD)
            page.get_by_placeholder("Confirm password").fill(_MASTER_PASSWORD)
            page.get_by_role("button", name="Set password").click()

            # The overview renders the seeded records: the unshared cloud row
            # is desktop-only (share status has no row for it), the tombstone
            # renders destroyed without probing anything.
            page.wait_for_selector("text=Workspaces", timeout=_UI_TIMEOUT_MS)
            page.wait_for_selector("text=Seeded Cloud WS", timeout=_UI_TIMEOUT_MS)
            page.wait_for_selector("text=Tombstoned WS", timeout=_UI_TIMEOUT_MS)
            page.wait_for_selector("text=desktop-only", timeout=_UI_TIMEOUT_MS)
            page.wait_for_selector("text=destroyed", timeout=_UI_TIMEOUT_MS)

            # -- Destroy the active record. There is no lease behind it (the
            # exact state of a workspace whose lease is already gone), so the
            # flow is: confirm dialog -> CAS-tombstone the record.
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Destroy").click()
            page.wait_for_function(
                "() => document.querySelectorAll('span').length > 0 && "
                "[...document.querySelectorAll('span')].filter(s => s.textContent === 'destroyed').length >= 2",
                timeout=_UI_TIMEOUT_MS,
            )
            # The tombstone is server-side, not just rendered.
            records_resp = context.request.get(f"{connector_url}/sync/records")
            assert records_resp.ok, records_resp.text()
            state_by_host = {row["host_id"]: row["state"] for row in records_resp.json()["records"]}
            assert state_by_host[active_host_id] == "destroyed"

            # -- Fresh browser session (new cookie jar, no sessionStorage):
            # the returning-account unlock path, wrong password first.
            second_context = browser.new_context()
            _signin_in_context(second_context, connector_url, email, password)
            second_page = second_context.new_page()
            second_page.goto(f"{connector_url}/web")
            second_page.wait_for_selector("text=Unlock your workspaces", timeout=_UI_TIMEOUT_MS)
            second_page.get_by_placeholder("Master password").fill("not-the-master-password")
            second_page.get_by_role("button", name="Unlock").click()
            second_page.wait_for_selector("text=Wrong master password.", timeout=_UI_TIMEOUT_MS)

            second_page.get_by_placeholder("Master password").fill(_MASTER_PASSWORD)
            second_page.get_by_role("button", name="Unlock").click()
            second_page.wait_for_selector("text=Workspaces", timeout=_UI_TIMEOUT_MS)
            second_page.wait_for_selector("text=Seeded Cloud WS", timeout=_UI_TIMEOUT_MS)
        finally:
            browser.close()
