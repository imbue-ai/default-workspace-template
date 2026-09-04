"""``minds_services`` tests for the REAL account-signup flow (no admin bypass).

Where ``test_logged_in_smoke.py`` provisions its user through the admin
``POST /admin/test-signup`` shortcut, these tests drive account creation the
way a brand-new user actually does it -- ``POST /accounts/api/signup`` against
the live connector -- and cover the parts of the signup surface that shortcut
skips:

* :func:`test_realistic_signup_verifies_email_via_mailtm` is the only
  deployment test that clicks a verification email through end to end: it signs
  up at a fresh mail.tm address, has the connector send its own verification
  email, polls the real mailbox for the token, and confirms the identity flips
  from unverified to verified. This is the ``signup_email`` (mail.tm) seam that
  nothing in ``deployment_tests/`` previously exercised.
* :func:`test_device_handoff_requires_explicit_confirmation` pins the gates
  around the loopback ``/accounts/authorize`` handoff (the ``?next=`` bounce to
  ``/login``) that ``test_accounts_web.py`` only walks the happy path of.
* :func:`test_signup_page_offers_google_and_email_paths` pins the public
  ``/accounts/api/config`` contract and the Google-vs-email rendering branch on
  the hosted ``/signup`` page.

They are ``release`` + ``minds_services`` because they need a live shared env
(connector + SuperTokens); the verification test additionally needs the
orchestrator's per-run mail.tm account (the ``signup_email`` fixture skips
cleanly when it is absent). Accounts created here are deleted on teardown via
``register_signup_user_for_cleanup`` -- the realistic addresses do not all
match the session sweep's ``test-<hex>@example.test`` pattern, so the tests
clean up after themselves rather than leaning on the sweep.

See ``real-signup-e2e-pattern.md`` (alongside this file) for the recipe this
file is meant to serve as the template for.
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

from imbue.minds.deployment_tests._mailtm import MailtmInbox
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.deployment_tests.primitives import MailtmFetchError
from imbue.minds.deployment_tests.primitives import VerificationToken

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_REQUEST_TIMEOUT_SECONDS = 30.0
# The non-mail tests only make HTTP calls to the live env, so 180s (matching
# test_logged_in_smoke) covers the cold-boot readiness poll plus the assertions.
_TEST_TIMEOUT_SECONDS = 180
# Real email delivery (SuperTokens' hosted service -> mail.tm) is the slow part
# of the verification test, so it gets a wide budget: ~120s of env-ready polling
# plus up to _VERIFICATION_RESEND_ROUNDS x _VERIFICATION_EMAIL_WAIT_SECONDS of
# waiting + resending below.
_VERIFICATION_TEST_TIMEOUT_SECONDS = 600
# How long to wait for a verification email after each send. Exceeds the
# connector's 60s per-user send cooldown so a resend after a missed round
# actually dispatches a fresh email rather than being suppressed.
_VERIFICATION_EMAIL_WAIT_SECONDS = 90.0
# How many send + wait rounds to attempt. SuperTokens' hosted email path is
# best-effort AND the SDK swallows send failures (send-verification returns
# sent:true regardless), so a single dispatch can silently not deliver; each
# missed round triggers a fresh send to self-heal.
_VERIFICATION_RESEND_ROUNDS = 4


def _fresh_password() -> str:
    """A password that satisfies the SuperTokens default policy (>=8 chars, a letter + a digit)."""
    return f"Vt1-{uuid4().hex}"


def _sweepable_email() -> str:
    """A throwaway address matching the conftest stale-user sweep pattern (``test-<hex>@example.test``)."""
    return f"test-{uuid4().hex}@example.test"


def _pkce_pair() -> tuple[str, str]:
    """A PKCE ``(verifier, S256 challenge)`` pair, same shape the device handoff uses."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _obtain_verification_token(client: httpx.Client, connector_url: str, inbox: MailtmInbox) -> VerificationToken:
    """Poll the mailbox for the verification token, resending after the cooldown on a miss.

    The caller has already dispatched the first email. Each round waits
    ``_VERIFICATION_EMAIL_WAIT_SECONDS`` (which exceeds the 60s send cooldown);
    on a miss it POSTs a fresh ``send-verification`` and waits again. This
    self-heals a single silently-dropped send (the SuperTokens SDK swallows send
    failures), but a sustained outage of the hosted email path exhausts the
    rounds and re-raises, tripping the test's flaky mark.
    """
    for attempt in range(_VERIFICATION_RESEND_ROUNDS):
        try:
            return inbox.wait_for_verification_token(timeout_seconds=_VERIFICATION_EMAIL_WAIT_SECONDS)
        except MailtmFetchError:
            if attempt == _VERIFICATION_RESEND_ROUNDS - 1:
                raise
            resend = client.post(f"{connector_url}/accounts/api/send-verification")
            assert resend.status_code == 200, resend.text
    raise AssertionError("unreachable: the loop returns a token or re-raises on the final round")


@pytest.mark.flaky
@pytest.mark.timeout(_VERIFICATION_TEST_TIMEOUT_SECONDS)
def test_realistic_signup_verifies_email_via_mailtm(
    shared_env: Callable[[str], SharedEnvHandle],
    signup_email: MailtmInbox,
    register_signup_user_for_cleanup: Callable[[str], None],
) -> None:
    """A brand-new account signs up, receives a real verification email, and clicks it through.

    No admin bypass and no pre-verified fixture: this drives the whole
    verification loop against a real mailbox. Sign up at a fresh mail.tm address
    via ``POST /accounts/api/signup``; confirm the account is unverified; have
    the connector send its own verification email
    (``POST /accounts/api/send-verification``); poll the mail.tm inbox for the
    emailed token (resending after the cooldown on a miss); consume it at
    ``POST /accounts/api/verify-email``; and confirm ``/accounts/api/me`` flips
    to verified.

    Marked ``flaky``: the verification email rides SuperTokens' best-effort
    hosted service (``api.supertokens.io``), and the SDK swallows send failures
    (send-verification returns sent:true regardless), so delivery is not
    guaranteed. Direct probes show it delivers reliably from the connector's
    Modal workspace, so misses are rare, and ``_obtain_verification_token``
    resends to absorb a single dropped send -- but a sustained hosted-email
    outage can still fail the run. Witnessing the send/deliver path for real
    (rather than tolerating its absence) is the deliberate trade here.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    email = str(signup_email.address)
    password = _fresh_password()

    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        # Real browser signup establishes the cookie session. Turnstile is
        # disabled on test tiers, so the empty token is accepted (a configured
        # tier would refuse this and the test would need a seeded account).
        signup = client.post(
            f"{connector_url}/accounts/api/signup",
            json={"email": email, "password": password, "turnstile_token": ""},
        )
        assert signup.status_code == 200, signup.text
        signup_body = signup.json()
        assert signup_body["status"] == "OK", signup_body
        register_signup_user_for_cleanup(signup_body["user"]["user_id"])

        # A fresh account is signed in but unverified: verification is
        # non-blocking, so signup never sends an email on its own.
        me_before = client.get(f"{connector_url}/accounts/api/me")
        assert me_before.status_code == 200, me_before.text
        assert me_before.json()["email"] == email, me_before.text
        assert me_before.json()["email_verified"] is False, me_before.text

        # The browser session asks the connector to send ITS verification email.
        # A brand-new user is never on the cooldown, so this first send really
        # dispatches (sent:true documents that contract).
        send = client.post(f"{connector_url}/accounts/api/send-verification")
        assert send.status_code == 200, send.text
        assert send.json() == {"status": "OK", "sent": True, "already_verified": False}, send.text

        # Extract the verification token from the inbox, resending after the
        # cooldown if a best-effort send silently dropped.
        token = _obtain_verification_token(client, connector_url, signup_email)

        # Consuming the emailed token verifies the address (the click-through).
        verify = client.post(f"{connector_url}/accounts/api/verify-email", json={"token": str(token)})
        assert verify.status_code == 200, verify.text
        assert verify.json() == {"status": "OK"}, verify.text

        me_after = client.get(f"{connector_url}/accounts/api/me")
        assert me_after.status_code == 200, me_after.text
        assert me_after.json()["email_verified"] is True, me_after.text


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_device_handoff_requires_explicit_confirmation(
    shared_env: Callable[[str], SharedEnvHandle],
    register_signup_user_for_cleanup: Callable[[str], None],
) -> None:
    """``/accounts/authorize`` never mints a code without a session AND explicit confirmation.

    Complements ``test_browser_login_contract_over_http`` (the happy-path
    exchange) by pinning the gates: a non-loopback ``redirect_uri`` and missing
    PKCE/state are refused outright, and the two ways the flow bounces back to
    ``/login`` -- no session, and a session without ``confirmed=1`` -- keep the
    handoff behind an explicit user action even though ``confirmed`` is
    client-supplied.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    _verifier, challenge = _pkce_pair()
    redirect_uri = "http://127.0.0.1:8321/callback"
    state = f"state-{uuid4().hex}"
    authorize_query = urlencode({"redirect_uri": redirect_uri, "code_challenge": challenge, "state": state})

    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        # A non-loopback redirect_uri is refused outright: the code may only
        # ever go to a listener on the user's own machine.
        bad_redirect = client.get(
            f"{connector_url}/accounts/authorize?"
            + urlencode(
                {"redirect_uri": "https://evil.example.com/callback", "code_challenge": challenge, "state": state}
            ),
            follow_redirects=False,
        )
        assert bad_redirect.status_code == 400, bad_redirect.text

        # A loopback redirect_uri missing the PKCE challenge / state is refused.
        missing_params = client.get(
            f"{connector_url}/accounts/authorize?" + urlencode({"redirect_uri": redirect_uri}),
            follow_redirects=False,
        )
        assert missing_params.status_code == 400, missing_params.text

        # With no browser session, a well-formed authorize bounces to /login and
        # carries itself as the next= target -- with no confirmed flag, so the
        # user has to come back through an explicit action.
        unauth = client.get(f"{connector_url}/accounts/authorize?{authorize_query}", follow_redirects=False)
        assert unauth.status_code == 302, unauth.text
        unauth_location = unauth.headers["location"]
        assert unauth_location.startswith("/login?next="), unauth_location
        unauth_next = parse_qs(urlsplit(unauth_location).query)["next"][0]
        assert unauth_next.startswith("/accounts/authorize"), unauth_next
        assert "confirmed" not in unauth_next, unauth_next

        # Establish a real browser session via signup (swept address, and also
        # registered for deterministic cleanup).
        email = _sweepable_email()
        signup = client.post(
            f"{connector_url}/accounts/api/signup",
            json={"email": email, "password": _fresh_password(), "turnstile_token": ""},
        )
        assert signup.status_code == 200, signup.text
        assert signup.json()["status"] == "OK", signup.text
        register_signup_user_for_cleanup(signup.json()["user"]["user_id"])

        # WITH a session but WITHOUT confirmed=1, authorize still refuses to mint
        # a code and bounces to /login: the "Continue as ..." interstitial is
        # mandatory, so a crafted link can't silently complete the handoff.
        unconfirmed = client.get(f"{connector_url}/accounts/authorize?{authorize_query}", follow_redirects=False)
        assert unconfirmed.status_code == 302, unconfirmed.text
        assert unconfirmed.headers["location"].startswith("/login?next="), unconfirmed.headers["location"]

        # Only WITH confirmed=1 does it mint the one-time code and bounce to the
        # loopback callback carrying the echoed state (the code exchange itself
        # is covered by test_browser_login_contract_over_http).
        confirmed_query = urlencode(
            {"redirect_uri": redirect_uri, "code_challenge": challenge, "state": state, "confirmed": "1"}
        )
        confirmed = client.get(f"{connector_url}/accounts/authorize?{confirmed_query}", follow_redirects=False)
        assert confirmed.status_code == 302, confirmed.text
        confirmed_location = confirmed.headers["location"]
        assert confirmed_location.startswith(redirect_uri), confirmed_location
        callback_params = parse_qs(urlsplit(confirmed_location).query)
        assert callback_params["state"] == [state], confirmed_location
        assert callback_params["code"][0], confirmed_location


@pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
def test_signup_page_offers_google_and_email_paths(
    shared_env: Callable[[str], SharedEnvHandle],
) -> None:
    """The config contract and hosted ``/signup`` page expose the Google + email paths per tier.

    Asserts the public ``/accounts/api/config`` shape (a boolean
    ``google_enabled``; ``turnstile_site_key`` is a string, empty on test
    tiers), then -- when Playwright's chromium is available -- that the rendered
    ``/signup`` honors ``google_enabled``: a Google-configured tier leads with
    the Google button and collapses the email form behind the reveal link,
    while a Google-less tier renders the email form directly. No account is
    created, so nothing needs cleanup.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")

    with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        config = client.get(f"{connector_url}/accounts/api/config")
        assert config.status_code == 200, config.text
        config_body = config.json()
        assert isinstance(config_body["google_enabled"], bool), config_body
        # Turnstile is disabled on ci/dev tiers (no TURNSTILE_SECRET_KEY); the
        # public site key is typically empty there. Assert only the type so the
        # test does not break on a tier that happens to publish a site key.
        assert isinstance(config_body["turnstile_site_key"], str), config_body
    google_enabled = config_body["google_enabled"]

    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except playwright_api.Error as exc:
            pytest.skip(f"Playwright chromium is not installed on this runner: {exc}")
        try:
            page = browser.new_page()
            page.goto(f"{connector_url}/signup")
            # Wait for the built bundle to render one of the mutually-exclusive
            # entry points (never the 503 "not built" placeholder).
            page.wait_for_selector("#google-signin-btn,#reveal-email-form-btn,#email", timeout=30_000)
            if google_enabled:
                # Google-first: the button renders and the email form stays
                # collapsed behind the reveal link until the user asks for it.
                assert page.locator("#google-signin-btn").count() == 1, page.content()
                assert page.locator("#reveal-email-form-btn").count() == 1, page.content()
                assert page.locator("#email").count() == 0, page.content()
                page.click("#reveal-email-form-btn")
                page.wait_for_selector("#email", timeout=30_000)
                assert page.locator("#auth-submit-btn").count() == 1, page.content()
            else:
                # No Google on this tier: the email form is the only path and
                # renders expanded from the start.
                assert page.locator("#google-signin-btn").count() == 0, page.content()
                page.wait_for_selector("#email", timeout=30_000)
                assert page.locator("#auth-submit-btn").count() == 1, page.content()
        finally:
            browser.close()
