"""The request identity a proxy vouches for: the ``X-Imbue-Identity`` header every request into the workspace
carries (the share identity spec, section 4.2), read the way the shell reads it, and the display name neko shows a
session under."""

from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from screen.data_types import RequestIdentity
from screen.primitives import DisplayName

IDENTITY_HEADER: Final[str] = "X-Imbue-Identity"
AGENT_ID_HEADER: Final[str] = "X-Mngr-Agent-Id"
AGENT_NAME_HEADER: Final[str] = "X-Mngr-Agent-Name"

ANONYMOUS_OWNER: Final[RequestIdentity] = RequestIdentity(owner=True)

# What a session is called when its identity carries no name and no email: the owner reached over loopback (an agent
# or a test, which the proxies never stamp), or a visitor an older proxy admitted without an account.
OWNER_FALLBACK_NAME: Final[DisplayName] = DisplayName("Owner")
GUEST_FALLBACK_NAME: Final[DisplayName] = DisplayName("Guest")
MAX_DISPLAY_NAME_LENGTH: Final[int] = 64


@pure
def parse_identity_header(header_value: str | None) -> RequestIdentity:
    """The identity a request carries; a missing or unreadable header reads as the anonymous owner.

    A missing header means the request came through no current proxy (an older forward, an in-container caller): the
    gateway and the forwarder both stamp one, so it is the owner, never a visitor. An unreadable one is logged and
    read the same way.
    """
    if header_value is None or not header_value.strip():
        return ANONYMOUS_OWNER
    try:
        return RequestIdentity.model_validate_json(header_value)
    except ValidationError as e:
        logger.warning("Ignored an unreadable {} header: {}", IDENTITY_HEADER, e.errors()[0]["msg"])
        return ANONYMOUS_OWNER


@pure
def display_name_for_identity(identity: RequestIdentity) -> DisplayName:
    """What neko shows the session as: the name, else the email's local part, else Owner or Guest."""
    if identity.name is not None and identity.name.strip():
        return DisplayName(identity.name.strip()[:MAX_DISPLAY_NAME_LENGTH])
    if identity.email is not None and identity.email.strip():
        local_part = identity.email.strip().partition("@")[0]
        if local_part:
            return DisplayName(local_part[:MAX_DISPLAY_NAME_LENGTH])
    return OWNER_FALLBACK_NAME if identity.owner else GUEST_FALLBACK_NAME
