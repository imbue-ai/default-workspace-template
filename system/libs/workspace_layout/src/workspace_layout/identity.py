"""The request identity every request into the workspace carries: the ``X-Imbue-Identity`` header a proxy stamps
(the share identity spec, section 4.2; the share gateway README, "Request identity"), parsed once, and the
participant key it names (the multiplayer spec, section 4.1), read the same way by the shell and the apps."""

from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from workspace_layout.primitives import OWNER_PARTICIPANT_KEY
from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import UserId

IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"


class RequestIdentity(FrozenModel):
    """The requester a proxy vouched for: the owner flag always; the account (its id and email) when the workspace is
    shared; the grant (its agent id, name, and granter) for a visiting agent. What to call an account and what it
    looks like is its profile, not the header's."""

    # The header is cross-version wire data from the proxies; a field this build does not
    # know -- or no longer reads, like the display name older proxies stamp -- must never make it unreadable.
    model_config = ConfigDict(extra="ignore")

    # Strict: a proxy stamps a real boolean, and a header that spells one as text is not a proxy's.
    owner: bool = Field(strict=True, description="Whether the requester is the workspace's owner")
    user_id: str | None = Field(default=None, description="The account's user id (absent for an unshared workspace)")
    email: str | None = Field(
        default=None, description="The account's verified email, present exactly when user_id is"
    )
    agent_id: str | None = Field(default=None, description="A visiting agent's grant id; absent for a person")
    agent_name: str | None = Field(default=None, description="The name the visiting agent joined under")
    granter: str | None = Field(default=None, description="Who invited the visiting agent, as the grant records it")


ANONYMOUS_OWNER: Final[RequestIdentity] = RequestIdentity(owner=True)


@pure
def parse_identity_header(header_value: str | None) -> RequestIdentity:
    """The identity a request carries; a missing or unreadable header reads as the anonymous owner.

    A missing header means the request came through no current proxy (an older
    forward, an in-container caller): it is treated as the owner with no account,
    never as a visitor. An unreadable one is logged and treated the same way.
    """
    if header_value is None or not header_value.strip():
        return ANONYMOUS_OWNER
    try:
        return RequestIdentity.model_validate_json(header_value)
    except ValueError as e:
        logger.warning("Ignored an unreadable {} header: {}", IDENTITY_HEADER, e)
        return ANONYMOUS_OWNER


@pure
def participant_key_of(identity: RequestIdentity) -> ParticipantKey:
    """The participant the request comes from: the account when the header names one, the visiting agent when it
    names a grant, and otherwise the owner of a workspace that has not shared (the multiplayer spec, S12)."""
    if identity.user_id is not None:
        return ParticipantKey(f"user:{UserId(identity.user_id)}")
    if identity.agent_id is not None:
        return ParticipantKey(f"agent:{identity.agent_id}")
    return OWNER_PARTICIPANT_KEY


@pure
def is_owner_participant(identity: RequestIdentity) -> bool:
    """Whether the requester is the owner, under either of the owner's keys (S12): the header says so, or it names
    no account and no grant, which ``participant_key_of`` keys ``owner`` whatever the flag says."""
    return identity.agent_id is None and (identity.owner or identity.user_id is None)


@pure
def visiting_user_id(identity: RequestIdentity) -> UserId | None:
    """The user id of a signed-in requester other than the owner, the one kind of requester who gets a desktop of
    their own on arrival (desktop plan section 3.10); None for the owner and for a requester with no account."""
    if identity.owner or identity.user_id is None:
        return None
    return UserId(identity.user_id)
