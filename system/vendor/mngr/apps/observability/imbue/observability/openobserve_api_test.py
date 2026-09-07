import base64

import pytest

from imbue.observability.mock_openobserve_api_test import MockOpenObserveApi
from imbue.observability.openobserve_api import OpenObserveApiError
from imbue.observability.openobserve_api import _dashboard_summary_from_listing_entry
from imbue.observability.openobserve_api import apply_log_stream_retention
from imbue.observability.openobserve_api import build_basic_authorization_header
from imbue.observability.openobserve_api import ensure_sender_credentials
from imbue.observability.openobserve_api import sender_email
from imbue.observability.primitives import SenderClass


def test_basic_authorization_header_round_trips() -> None:
    header = build_basic_authorization_header("a@example.com", "pw-1")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    assert decoded == "a@example.com:pw-1"


def test_ensure_sender_credentials_mints_every_missing_sender() -> None:
    api = MockOpenObserveApi()
    credentials = ensure_sender_credentials(api, {sender: "" for sender in SenderClass})

    assert set(credentials) == set(SenderClass)
    assert all(credential.is_newly_minted for credential in credentials.values())
    assert sorted(email for email, _password, _role in api.created_users) == sorted(
        sender_email(sender) for sender in SenderClass
    )
    # Each minted credential authenticates as its own user with the password
    # that was actually created, so the Vault write-back is usable as-is.
    for sender_class, credential in credentials.items():
        matching = [entry for entry in api.created_users if entry[0] == sender_email(sender_class)]
        assert len(matching) == 1
        _email, password, _role = matching[0]
        assert credential.authorization_header_value.get_secret_value() == build_basic_authorization_header(
            sender_email(sender_class), password
        )


def test_ensure_sender_credentials_passwords_satisfy_openobserve_complexity_policy() -> None:
    # OpenObserve rejects user creation unless the password carries at least
    # one lowercase letter, one uppercase letter, one digit, and one special
    # character (observed live on v0.92.2 during the dev bring-up).
    api = MockOpenObserveApi()

    ensure_sender_credentials(api, {sender: "" for sender in SenderClass})

    assert len(api.created_users) == len(SenderClass)
    for _email, password, _role in api.created_users:
        assert 8 <= len(password) <= 128
        assert any(character.islower() for character in password)
        assert any(character.isupper() for character in password)
        assert any(character.isdigit() for character in password)
        assert any(character in "!@#$%^&*" for character in password)


def test_ensure_sender_credentials_preserves_existing_vault_values() -> None:
    # Re-provisioning must never rotate a credential behind the fleet: an
    # existing Vault value is kept verbatim and no user is touched.
    api = MockOpenObserveApi(user_emails=[sender_email(SenderClass.BOXES)])
    existing = {SenderClass.MODAL: "", SenderClass.BOXES: "Basic existing", SenderClass.RELAYS: ""}

    credentials = ensure_sender_credentials(api, existing)

    assert credentials[SenderClass.BOXES].is_newly_minted is False
    assert credentials[SenderClass.BOXES].authorization_header_value.get_secret_value() == "Basic existing"
    created_emails = [email for email, _password, _role in api.created_users]
    assert sender_email(SenderClass.BOXES) not in created_emails
    assert credentials[SenderClass.MODAL].is_newly_minted is True


def test_ensure_sender_credentials_refuses_an_orphaned_user() -> None:
    # The user exists but Vault lost its credential: minting silently would
    # require a password reset we deliberately do not automate.
    api = MockOpenObserveApi(user_emails=[sender_email(SenderClass.MODAL)])

    with pytest.raises(OpenObserveApiError, match="exists but the tier Vault entry has no credential"):
        ensure_sender_credentials(api, {sender: "" for sender in SenderClass})


def test_apply_log_stream_retention_reports_missing_streams_as_skipped() -> None:
    api = MockOpenObserveApi(existing_stream_names=["modal_logs", "box_logs"])

    is_applied_by_stream = apply_log_stream_retention(api, 90)

    assert is_applied_by_stream["modal_logs"] is True
    assert is_applied_by_stream["relay_logs"] is False
    # Every override targets the logs stream type with the requested days.
    assert all(stream_type == "logs" and days == 90 for _name, stream_type, days in api.retention_updates)


def test_dashboard_summary_reads_the_schema_version_nested_document() -> None:
    summary = _dashboard_summary_from_listing_entry(
        {"version": 5, "hash": "abc", "v5": {"dashboardId": "dash-1", "title": "Fleet version mix"}}
    )

    assert summary.dashboard_id == "dash-1"
    assert summary.title == "Fleet version mix"


def test_dashboard_summary_falls_back_to_a_flat_document() -> None:
    summary = _dashboard_summary_from_listing_entry({"dashboardId": "dash-2", "title": "Legacy flat"})

    assert summary.dashboard_id == "dash-2"
    assert summary.title == "Legacy flat"
