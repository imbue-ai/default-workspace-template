"""``minds_services`` tests: the hosted accounts surface on a live env.

Two layers:

1. An HTTP-level walk of the whole browser-login contract (cookie-based
   signup, the confirmed authorize -> loopback code redirect, the PKCE code
   exchange, and using the minted device token against an authed route) --
   the same wire flow ``mngr imbue_cloud auth login`` drives, without a
   browser in the loop.
2. A Playwright pass over the real pages: the built bundle renders the
   sign-up form, a fresh account signs up through it, and lands signed in on
   the account page. Skipped when the Playwright browser is not installed on
   the runner (``playwright install chromium``).

Signups here use throwaway ``test-<hex>@example.test`` addresses, the exact
shape the conftest's stale-test-user sweep matches, so leftover accounts from
a crashed run are deleted by the next session; verification is non-blocking
so no mailbox is needed.
"""

import base64
import hashlib
import secrets
from collections.abc import Callable
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_REQUEST_TIMEOUT_SECONDS = 30.0
_TEST_TIMEOUT_SECONDS = 180


def _fresh_credentials() -> tuple[str, str]:
    # The address MUST match conftest's _STALE_TEST_USER_EMAIL_PATTERN
    # (test-<hex>@example.test) or the sweep never deletes these accounts and
    # they accumulate on the shared env across runs.
    return f"test-{uuid4().hex}@example.test", f"pw-{uuid4().hex}"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_browser_login_contract_over_http(shared_env: Callable[[str], SharedEnvHandle]) -> None:
    """Walk the hosted login's wire contract end to end without a browser.

    Covers: cookie-session signup, /accounts/api/me identity, the confirmed
    /accounts/authorize redirect to a loopback callback carrying a one-time
    code, the PKCE exchange at /auth/device/token, the minted device token
    working against an authed route, single-use code enforcement, and the
    device-scoped sign-out.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    email, password = _fresh_credentials()
    verifier, challenge = _pkce_pair()
    redirect_uri = "http://127.0.0.1:8321/callback"
    state = f"state-{uuid4().hex}"

    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        # Browser signup establishes the cookie session (Turnstile is
        # disabled on test tiers; a configured tier would refuse this and
        # the test should then use a seeded account instead).
        signup = client.post(
            f"{connector_url}/accounts/api/signup",
            json={"email": email, "password": password, "turnstile_token": ""},
        )
        assert signup.status_code == 200, signup.text
        assert signup.json()["status"] == "OK", signup.text

        me = client.get(f"{connector_url}/accounts/api/me")
        assert me.status_code == 200, me.text
        assert me.json()["email"] == email
        assert me.json()["email_verified"] is False

        # A confirmed authorize mints the one-time code and bounces to the
        # loopback callback (not followed -- there is no listener here).
        authorize_query = urlencode(
            {"redirect_uri": redirect_uri, "code_challenge": challenge, "state": state, "confirmed": "1"}
        )
        authorize = client.get(f"{connector_url}/accounts/authorize?{authorize_query}", follow_redirects=False)
        assert authorize.status_code == 302, authorize.text
        location = authorize.headers["location"]
        assert location.startswith(redirect_uri), location
        callback_params = parse_qs(urlsplit(location).query)
        assert callback_params["state"] == [state]
        code = callback_params["code"][0]

        # The exchange mints a fresh device session (independent tokens).
        exchange = client.post(
            f"{connector_url}/auth/device/token",
            json={"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
        )
        assert exchange.status_code == 200, exchange.text
        exchange_body = exchange.json()
        assert exchange_body["status"] == "OK", exchange_body
        assert exchange_body["user"]["email"] == email
        device_access_token = exchange_body["tokens"]["access_token"]
        assert device_access_token and exchange_body["tokens"]["refresh_token"]

        # The device token authenticates real routes (verification is
        # non-blocking, so the fresh unverified account works).
        shares = client.get(
            f"{connector_url}/shares",
            headers={"Authorization": f"Bearer {device_access_token}"},
        )
        assert shares.status_code == 200, shares.text

        # Single use: replaying the code is refused.
        replay = client.post(
            f"{connector_url}/auth/device/token",
            json={"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
        )
        assert replay.status_code == 400, replay.text

        # Device-scoped sign-out kills only the device token; the browser
        # session (cookie jar) still answers /accounts/api/me.
        revoke = client.post(
            f"{connector_url}/auth/session/revoke-current",
            headers={"Authorization": f"Bearer {device_access_token}"},
        )
        assert revoke.status_code == 200, revoke.text
        me_after = client.get(f"{connector_url}/accounts/api/me")
        assert me_after.status_code == 200, me_after.text


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_hosted_pages_signup_via_playwright(shared_env: Callable[[str], SharedEnvHandle]) -> None:
    """Drive the real hosted pages: the bundle renders, sign-up works, and lands on the web client."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    email, password = _fresh_credentials()

    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright_api.Error as exc:
            pytest.skip(f"Playwright chromium is not installed on this runner: {exc}")
        try:
            page = browser.new_page()
            # No ?next=: a signup with no pending handoff lands on /web.
            page.goto(f"{connector_url}/signup")
            # The built bundle must actually be deployed (not the 503
            # placeholder). With Google configured on the tier, the signup tab
            # leads with the Google button and keeps the email/password fields
            # collapsed behind a reveal link; click it when present.
            page.wait_for_selector("#auth-submit-btn,#reveal-email-form-btn", timeout=30_000)
            if page.locator("#reveal-email-form-btn").count() > 0:
                page.click("#reveal-email-form-btn")
            page.wait_for_selector("#auth-submit-btn", timeout=30_000)
            # The plan selector renders with Explorer preselected; keep it.
            assert page.locator("#plan-select").input_value() == "explorer"
            page.fill("#email", email)
            page.fill("#password", password)
            page.fill("#confirm-password", password)
            # Submitting without the terms agreement must be refused with a
            # visible error, not create an account.
            page.click("#auth-submit-btn")
            page.wait_for_selector("text=please check the box", timeout=30_000)
            assert page.url.startswith(f"{connector_url}/signup")
            page.check("#terms-checkbox")
            page.click("#auth-submit-btn")
            # A signup with no pending handoff lands on the web client (the
            # product), not the account page.
            page.wait_for_url(f"{connector_url}/web", timeout=30_000)
            # The web-chrome bundle must actually be deployed (not the 503
            # "not built" placeholder).
            assert "not built" not in page.content()
            # The account page still works for the fresh session.
            page.goto(f"{connector_url}/manage")
            page.wait_for_selector("#signout-btn", timeout=30_000)
            assert email in page.content()
        finally:
            browser.close()
