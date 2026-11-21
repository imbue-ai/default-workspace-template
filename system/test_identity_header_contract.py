"""The share gateway writes the ``X-Imbue-Identity`` header and every app reads it through the app SDK.

The gateway keeps its own name for the header and its own renderer, since it is a small service
that loads neither pydantic nor the SDK; these tests are what hold the two sides to one contract.
"""

from imbue.app_sdk import identity as sdk_identity
from imbue.app_sdk.identity import parse_identity_header, participant_key_of
from share_gateway import identity as gateway_identity
from share_gateway.identity import (
    AgentIdentity,
    RequesterIdentity,
    render_agent_identity_header,
    render_identity_header,
)


def test_the_gateway_and_the_apps_name_the_header_the_same() -> None:
    assert gateway_identity.IDENTITY_HEADER == sdk_identity.IDENTITY_HEADER


def test_a_person_the_gateway_vouches_for_reads_as_that_person_in_an_app() -> None:
    header = render_identity_header(
        RequesterIdentity(user_id="user-bob-4471", email="bob@example.com", is_owner=False)
    )

    parsed = parse_identity_header(header)

    assert (parsed.owner, parsed.user_id, parsed.email, parsed.agent_id) == (
        False,
        "user-bob-4471",
        "bob@example.com",
        None,
    )
    assert parsed.is_header_present is True
    assert participant_key_of(parsed) == "user:user-bob-4471"


def test_the_owner_the_gateway_vouches_for_reads_as_the_owner_in_an_app() -> None:
    header = render_identity_header(
        RequesterIdentity(user_id="user-owner-9c21", email="owner@example.com", is_owner=True)
    )

    assert parse_identity_header(header).owner is True


def test_a_visiting_agent_the_gateway_vouches_for_reads_as_that_agent_in_an_app() -> None:
    header = render_agent_identity_header(
        AgentIdentity(agent_id="va-0123456789abcdef", agent_name="Bob's agent", granter="user:8c0f")
    )

    parsed = parse_identity_header(header)

    assert (parsed.owner, parsed.user_id, parsed.agent_id, parsed.agent_name, parsed.granter) == (
        False,
        None,
        "va-0123456789abcdef",
        "Bob's agent",
        "user:8c0f",
    )
    assert participant_key_of(parsed) == "agent:va-0123456789abcdef"
