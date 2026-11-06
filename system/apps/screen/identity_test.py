import json

from screen.identity import display_name_for_identity
from screen.identity import parse_identity_header


def test_missing_or_unreadable_header_is_the_anonymous_owner() -> None:
    assert parse_identity_header(None).owner is True
    assert parse_identity_header("   ").owner is True
    assert parse_identity_header("{not json").owner is True
    assert parse_identity_header(json.dumps({"user_id": "u1"})).owner is True


def test_header_fields_are_read_and_unknown_ones_ignored() -> None:
    identity = parse_identity_header(
        json.dumps({"owner": False, "user_id": "u1", "email": "mia@example.com", "extra": 1})
    )
    assert identity.owner is False
    assert identity.user_id == "u1"
    assert identity.email == "mia@example.com"


def test_display_name_prefers_the_name_then_the_email_local_part_then_the_role() -> None:
    assert display_name_for_identity(parse_identity_header(json.dumps({"owner": True, "name": " Josh "}))) == "Josh"
    assert (
        display_name_for_identity(parse_identity_header(json.dumps({"owner": False, "email": "mia@example.com"})))
        == "mia"
    )
    assert (
        display_name_for_identity(parse_identity_header(json.dumps({"owner": True, "email": "josh@imbue.com"})))
        == "josh"
    )
    assert display_name_for_identity(parse_identity_header(None)) == "Owner"
    assert display_name_for_identity(parse_identity_header(json.dumps({"owner": False}))) == "Guest"
    assert display_name_for_identity(parse_identity_header(json.dumps({"owner": False, "email": "@x"}))) == "Guest"
