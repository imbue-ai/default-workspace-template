"""Tests for the verified-email gate on remote-workspace creation (lease + claim).

An unverified account gets the structured ``email_not_verified`` 403 from
``POST /hosts/lease`` and ``POST /hosts/claim``, and the refusal itself sends
the verification email (under the server-side cooldown). Uses the browser
cookie-session path because the fake SuperTokens backend models real
(unverified-by-default) accounts there; the Bearer stub user is always
verified, which the plain lease tests already cover.
"""

from uuid import UUID

import pytest

from imbue.remote_service_connector.auth import UserAuth
from imbue.remote_service_connector.auth_proxy import require_verified_email_for_remote_workspace
from imbue.remote_service_connector.errors import EmailNotVerifiedError
from imbue.remote_service_connector.testing import _make_pool_quota_web_test_client
from imbue.remote_service_connector.testing import _sign_in_browser_user

_UNVERIFIED_EMAIL = "unverified-creator@example.com"


def _lease_body() -> dict[str, object]:
    return {
        "ssh_public_key": "ssh-ed25519 AAAA testkey",
        "host_name": "gate-test-workspace",
        "attributes": {"version": "v0.1.0"},
    }


def test_lease_refuses_an_unverified_account_and_sends_the_verification_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser_user(client, st_backend, _UNVERIFIED_EMAIL)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-00000000dd01"), version="v0.1.0")

    resp = client.post("/hosts/lease", json=_lease_body())

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "email_not_verified"
    assert detail["email"] == _UNVERIFIED_EMAIL
    assert detail["sent"] is True
    # The message must direct the user to the mail we just sent.
    assert "spam" in detail["message"]
    assert _UNVERIFIED_EMAIL in detail["message"]
    # The refusal sent exactly one verification email and took no lease.
    assert len(st_backend.sent_verification_emails) == 1
    assert backend.pool_rows[0].status == "available"


def test_lease_retry_within_the_cooldown_reports_the_send_as_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser_user(client, st_backend, _UNVERIFIED_EMAIL)

    first = client.post("/hosts/lease", json=_lease_body())
    second = client.post("/hosts/lease", json=_lease_body())

    assert first.json()["detail"]["sent"] is True
    assert second.status_code == 403
    second_detail = second.json()["detail"]
    assert second_detail["code"] == "email_not_verified"
    assert second_detail["sent"] is False
    # The cooldown suppressed the second send but the message still points at the inbox.
    assert len(st_backend.sent_verification_emails) == 1
    assert "spam" in second_detail["message"]


def test_lease_succeeds_once_the_email_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    client, backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    user_id = _sign_in_browser_user(client, st_backend, _UNVERIFIED_EMAIL)
    backend.add_available_host(host_id=UUID("00000000-0000-0000-0000-00000000dd02"), version="v0.1.0")
    st_backend.mark_email_verified(user_id)

    resp = client.post("/hosts/lease", json=_lease_body())

    assert resp.status_code == 200
    assert resp.json()["host_name"] == "gate-test-workspace"
    assert backend.pool_rows[0].status == "leased"
    # No verification email went out for the verified account.
    assert st_backend.sent_verification_emails == []


def test_claim_refuses_an_unverified_account_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    # No web-template env is configured: the verification gate must fire
    # before the pinned-template 503, so an unverified user always gets the
    # actionable refusal.
    client, _backend, _entitlements, _litellm, st_backend = _make_pool_quota_web_test_client(monkeypatch)
    _sign_in_browser_user(client, st_backend, _UNVERIFIED_EMAIL)

    resp = client.post(
        "/hosts/claim",
        json={"ssh_public_key": "ssh-ed25519 AAAA webkey", "host_name": "web-gate-test"},
    )

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "email_not_verified"
    assert detail["sent"] is True
    assert len(st_backend.sent_verification_emails) == 1


def test_gate_does_not_claim_a_send_for_an_account_without_an_email() -> None:
    """With no email on the account, nothing is sent and the message must not point at an inbox."""
    user = UserAuth(user_id_prefix="0123456789abcdef", email=None, is_email_verified=False)

    with pytest.raises(EmailNotVerifiedError) as exc_info:
        require_verified_email_for_remote_workspace(user, "0123456789abcdef-full-id")

    assert exc_info.value.email is None
    assert exc_info.value.is_verification_email_sent is False
    message = str(exc_info.value)
    assert "no email address on file" in message
    # The prose must not claim a delivery the user could go looking for.
    assert "inbox" not in message
    assert "emailed a verification link" not in message
