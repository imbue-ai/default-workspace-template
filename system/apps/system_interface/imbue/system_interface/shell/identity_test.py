"""Tests for the identity header: how it parses, and which requester counts as a visiting user."""

import json

import pytest

from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.identity import ANONYMOUS_OWNER
from imbue.system_interface.shell.identity import RequestIdentity
from imbue.system_interface.shell.identity import parse_identity_header
from imbue.system_interface.shell.identity import visiting_user_id


def test_parse_identity_header_reads_the_record_and_ignores_unknown_fields() -> None:
    header = json.dumps(
        {
            "owner": False,
            "user_id": "user-bob-4471",
            "email": "bob@example.com",
            "display_name": "Bob",
            "avatar_url": "https://a/b",
            "a_field_from_a_newer_proxy": 1,
        }
    )
    assert parse_identity_header(header) == RequestIdentity(
        owner=False, user_id="user-bob-4471", email="bob@example.com", display_name="Bob", avatar_url="https://a/b"
    )


def test_parse_identity_header_treats_absence_and_garbage_as_the_anonymous_owner() -> None:
    assert parse_identity_header(None) == ANONYMOUS_OWNER
    assert parse_identity_header("   ") == ANONYMOUS_OWNER
    assert parse_identity_header("not json") == ANONYMOUS_OWNER
    assert parse_identity_header('{"user_id": "x"}') == ANONYMOUS_OWNER
    assert parse_identity_header('{"owner": true}') == ANONYMOUS_OWNER
    assert ANONYMOUS_OWNER.user_id is None


def test_only_a_signed_in_requester_other_than_the_owner_is_a_visiting_user() -> None:
    assert visiting_user_id(ANONYMOUS_OWNER) is None
    assert visiting_user_id(RequestIdentity(owner=True, user_id="user-owner", email="owner@example.com")) is None
    assert visiting_user_id(RequestIdentity(owner=False)) is None
    assert visiting_user_id(RequestIdentity(owner=False, user_id="user-bob", email="bob@example.com")) == "user-bob"
    with pytest.raises(InvalidShellValueError):
        visiting_user_id(RequestIdentity(owner=False, user_id="../etc", email="x@example.com"))
