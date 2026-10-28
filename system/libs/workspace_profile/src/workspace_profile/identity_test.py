import json

from workspace_profile.identity import ANONYMOUS_OWNER, parse_identity_header


def test_a_missing_or_blank_header_reads_as_the_anonymous_owner() -> None:
    assert parse_identity_header(None) == ANONYMOUS_OWNER
    assert parse_identity_header("") == ANONYMOUS_OWNER
    assert parse_identity_header("   ") == ANONYMOUS_OWNER


def test_an_unreadable_header_reads_as_the_anonymous_owner() -> None:
    assert parse_identity_header("{not json") == ANONYMOUS_OWNER
    assert parse_identity_header(json.dumps({"owner": "yes"})) == ANONYMOUS_OWNER


def test_a_visitor_header_carries_their_account() -> None:
    identity = parse_identity_header(
        json.dumps(
            {"owner": False, "user_id": "user-1", "email": "visitor@example.com"}
        )
    )

    assert identity.owner is False
    assert identity.user_id == "user-1"
    assert identity.email == "visitor@example.com"


def test_unknown_header_fields_are_ignored() -> None:
    identity = parse_identity_header(
        json.dumps({"owner": True, "display_name": "A Newer Proxy's Field"})
    )

    assert identity.owner is True
    assert identity.user_id is None
