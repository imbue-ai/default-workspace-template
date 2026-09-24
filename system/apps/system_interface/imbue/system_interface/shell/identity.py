"""The request identity a proxy vouches for: the ``X-Imbue-Identity`` header every request into the workspace carries
(the share identity spec, section 4.2), parsed once for the routes that key behaviour on who is asking."""

from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.primitives import UserId

IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"


class RequestIdentity(FrozenModel):
    """The requester a proxy vouched for: the owner flag always, the account (its id and email) when the workspace is
    shared. What to call the account and what it looks like is its profile (``profiles.py``), not the header's."""

    # The header is cross-version wire data from the proxies; a field this build does not
    # know -- or no longer reads, like the display name older proxies stamp -- must never make it unreadable.
    model_config = ConfigDict(extra="ignore")

    owner: bool = Field(description="Whether the requester is the workspace's owner")
    user_id: str | None = Field(default=None, description="The account's user id (absent for an unshared workspace)")
    email: str | None = Field(
        default=None, description="The account's verified email, present exactly when user_id is"
    )


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
    except ValidationError as e:
        logger.warning("Ignored an unreadable {} header: {}", IDENTITY_HEADER, e.errors()[0]["msg"])
        return ANONYMOUS_OWNER


@pure
def visiting_user_id(identity: RequestIdentity) -> UserId | None:
    """The user id of a signed-in requester other than the owner, the one kind of requester who gets a desktop of
    their own on arrival (desktop plan section 3.10); None for the owner and for a requester with no account."""
    if identity.owner or identity.user_id is None:
        return None
    return UserId(identity.user_id)
