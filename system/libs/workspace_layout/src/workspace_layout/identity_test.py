import pytest

from workspace_layout.identity import ANONYMOUS_OWNER
from workspace_layout.identity import RequestIdentity
from workspace_layout.identity import is_owner_participant
from workspace_layout.identity import parse_identity_header
from workspace_layout.identity import participant_key_of
from workspace_layout.identity import visiting_user_id


def test_a_missing_or_unreadable_header_reads_as_the_anonymous_owner() -> None:
    assert parse_identity_header(None) == ANONYMOUS_OWNER
    assert parse_identity_header("   ") == ANONYMOUS_OWNER
    assert parse_identity_header("{not json") == ANONYMOUS_OWNER
    assert parse_identity_header('{"owner": "yes"}') == ANONYMOUS_OWNER
    assert parse_identity_header('{"owner": "false"}') == ANONYMOUS_OWNER


def test_a_person_header_keeps_the_account_and_ignores_fields_it_does_not_know() -> None:
    identity = parse_identity_header(
        '{"owner":false,"user_id":"user-bob-4471","email":"bob@example.com","display_name":"Bob"}'
    )
    assert identity == RequestIdentity(owner=False, user_id="user-bob-4471", email="bob@example.com")


def test_a_visiting_agent_header_carries_the_grant_and_no_user() -> None:
    identity = parse_identity_header(
        '{"owner":false,"agent_id":"va-0123456789abcdef0123456789abcdef","agent_name":"Josh\'s Claude","granter":"user:8c0f"}'
    )
    assert identity.user_id is None
    assert identity.agent_id == "va-0123456789abcdef0123456789abcdef"
    assert identity.agent_name == "Josh's Claude"
    assert identity.granter == "user:8c0f"


@pytest.mark.parametrize(
    ("header", "expected_key"),
    [
        ('{"owner":true}', "owner"),
        ('{"owner":true,"user_id":"user-owner-9c21","email":"owner@example.com"}', "user:user-owner-9c21"),
        ('{"owner":false,"user_id":"user-bob-4471","email":"bob@example.com"}', "user:user-bob-4471"),
        ('{"owner":false,"agent_id":"va-0123456789abcdef0123456789abcdef","agent_name":"Visitor","granter":"x"}', "agent:va-0123456789abcdef0123456789abcdef"),
    ],
)
def test_the_participant_key_follows_the_header(header: str, expected_key: str) -> None:
    assert participant_key_of(parse_identity_header(header)) == expected_key


def test_the_owner_is_the_owner_under_either_key_and_never_a_visiting_agent() -> None:
    assert is_owner_participant(ANONYMOUS_OWNER) is True
    assert is_owner_participant(RequestIdentity(owner=True, user_id="user-owner-9c21", email="o@example.com")) is True
    assert is_owner_participant(RequestIdentity(owner=False, user_id="user-bob-4471", email="b@example.com")) is False


def test_only_a_signed_in_visitor_has_a_visiting_user_id() -> None:
    assert visiting_user_id(ANONYMOUS_OWNER) is None
    assert visiting_user_id(RequestIdentity(owner=True, user_id="user-owner-9c21", email="o@example.com")) is None
    assert visiting_user_id(RequestIdentity(owner=False, user_id="user-bob-4471", email="b@example.com")) == "user-bob-4471"
