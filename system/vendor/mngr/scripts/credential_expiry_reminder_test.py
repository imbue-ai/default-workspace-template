from datetime import date

import pytest

from scripts.credential_expiry_reminder import CredentialExpiryReminderError
from scripts.credential_expiry_reminder import REGISTRY_DISPLAY_PATH
from scripts.credential_expiry_reminder import REGISTRY_PATH
from scripts.credential_expiry_reminder import build_issue_body
from scripts.credential_expiry_reminder import credentials_needing_reminder
from scripts.credential_expiry_reminder import issue_title_for_credential
from scripts.credential_expiry_reminder import parse_credential_registry

_VALID_REGISTRY = """
[[credentials]]
name = "example-token"
expires_on = 2027-08-24
rotation = "Mint a replacement and update the Vault leaf."
"""


def test_parse_credential_registry_parses_a_valid_entry() -> None:
    entries = parse_credential_registry(_VALID_REGISTRY)

    assert len(entries) == 1
    assert entries[0]["name"] == "example-token"
    assert entries[0]["expires_on"] == date(2027, 8, 24)


def test_parse_credential_registry_accepts_an_empty_registry() -> None:
    assert parse_credential_registry("") == []


def test_parse_credential_registry_rejects_a_quoted_date() -> None:
    registry_text = """
[[credentials]]
name = "example-token"
expires_on = "2027-08-24"
rotation = "Rotate it."
"""
    with pytest.raises(CredentialExpiryReminderError, match="bare TOML date"):
        parse_credential_registry(registry_text)


def test_parse_credential_registry_rejects_a_missing_rotation() -> None:
    registry_text = """
[[credentials]]
name = "example-token"
expires_on = 2027-08-24
"""
    with pytest.raises(CredentialExpiryReminderError, match="rotation"):
        parse_credential_registry(registry_text)


def test_parse_credential_registry_rejects_a_non_table_entry() -> None:
    with pytest.raises(CredentialExpiryReminderError, match="must be a"):
        parse_credential_registry('credentials = ["not-a-table"]')


def test_parse_credential_registry_rejects_a_duplicate_name() -> None:
    # The open-issue dedupe keys on the exact title derived from the name, so a
    # duplicate name would silently suppress one entry's reminder.
    registry_text = """
[[credentials]]
name = "example-token"
expires_on = 2027-08-24
rotation = "Rotate it."

[[credentials]]
name = "example-token"
expires_on = 2028-01-01
rotation = "Rotate it differently."
"""
    with pytest.raises(CredentialExpiryReminderError, match="duplicate credential name"):
        parse_credential_registry(registry_text)


def test_credentials_needing_reminder_selects_only_entries_inside_the_window() -> None:
    entries = [
        {"name": "far-out", "expires_on": date(2027, 8, 24), "rotation": "r"},
        {"name": "due-soon", "expires_on": date(2026, 9, 10), "rotation": "r"},
        {"name": "already-expired", "expires_on": date(2026, 8, 1), "rotation": "r"},
    ]

    due = credentials_needing_reminder(entries, date(2026, 8, 24))

    assert [entry["name"] for entry in due] == ["due-soon", "already-expired"]


def test_credentials_needing_reminder_includes_the_window_boundary_day() -> None:
    entries = [{"name": "boundary", "expires_on": date(2026, 9, 23), "rotation": "r"}]

    assert credentials_needing_reminder(entries, date(2026, 8, 24)) == entries


def test_issue_title_is_stable_for_dedupe() -> None:
    # The open-issue dedupe matches this exact title, so it must stay a pure
    # function of the credential name.
    assert issue_title_for_credential("example-token") == "credential expiring: example-token"


def test_issue_body_carries_the_date_rotation_and_registry_pointer() -> None:
    entry = {"name": "example-token", "expires_on": date(2027, 8, 24), "rotation": "Mint a replacement."}

    body = build_issue_body(entry)

    assert "2027-08-24" in body
    assert "Mint a replacement." in body
    assert REGISTRY_DISPLAY_PATH in body


def test_the_committed_registry_parses() -> None:
    # The real registry file must always satisfy the schema this script
    # enforces, or the weekly run fails instead of reminding.
    entries = parse_credential_registry(REGISTRY_PATH.read_text())

    assert any(entry["name"] == "mngr-openobserve-alerts" for entry in entries)
