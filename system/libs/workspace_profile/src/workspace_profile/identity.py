"""The request identity a proxy vouches for: the ``X-Imbue-Identity`` header every request into the workspace carries.

The same contract the shell's ``system_interface.shell.identity`` reads (the share identity
spec, section 4.2), kept here so the profile resolution and every app that consumes resolved
settings can answer "who is asking" from one definition. A missing, blank, or unreadable
header always reads as the anonymous owner, never as a visitor: it means the request came
through no current proxy (an older forward, an in-container caller).
"""

from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from loguru import logger
from pydantic import ConfigDict, Field, ValidationError

IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"


class RequestIdentity(FrozenModel):
    """The requester a proxy vouched for: the owner flag always, the account when the workspace is shared."""

    # The header is cross-version wire data from the proxies; a field this build does not
    # know must never make it unreadable.
    model_config = ConfigDict(extra="ignore")

    owner: bool = Field(description="Whether the requester is the workspace's owner")
    user_id: str | None = Field(
        default=None,
        description="The account's user id (absent for an unshared workspace)",
    )
    email: str | None = Field(
        default=None,
        description="The account's verified email, present exactly when user_id is",
    )


ANONYMOUS_OWNER: Final[RequestIdentity] = RequestIdentity(owner=True)


@pure
def parse_identity_header(header_value: str | None) -> RequestIdentity:
    """The identity a request carries; a missing or unreadable header reads as the anonymous owner."""
    if header_value is None or not header_value.strip():
        return ANONYMOUS_OWNER
    try:
        return RequestIdentity.model_validate_json(header_value)
    except ValidationError as e:
        logger.warning(
            "Ignored an unreadable {} header: {}", IDENTITY_HEADER, e.errors()[0]["msg"]
        )
        return ANONYMOUS_OWNER
