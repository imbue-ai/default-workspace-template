"""The request identity a proxy vouches for: the ``X-Imbue-Identity`` header every request into the workspace
carries (the share identity spec, section 4.2), read as the shell reads it (``workspace_layout.identity``; the
provenance check that admits a header-less request from loopback alone is installed on the app by
``app_schema.request_guard``), the agent headers the agent verbs name their caller with, and the display name neko
shows a session under."""

from typing import Final

from imbue.imbue_common.pure import pure
from screen.primitives import DisplayName
from workspace_layout.identity import RequestIdentity

AGENT_ID_HEADER: Final[str] = "X-Mngr-Agent-Id"
AGENT_NAME_HEADER: Final[str] = "X-Mngr-Agent-Name"

# What a session is called when its identity carries no email: the owner reached over loopback (an agent or a test,
# which the proxies never stamp), or a visitor an older proxy admitted without an account.
OWNER_FALLBACK_NAME: Final[DisplayName] = DisplayName("Owner")
GUEST_FALLBACK_NAME: Final[DisplayName] = DisplayName("Guest")
MAX_DISPLAY_NAME_LENGTH: Final[int] = 64


@pure
def display_name_for_identity(identity: RequestIdentity) -> DisplayName:
    """What neko shows the session as: the email's local part, else Owner or Guest."""
    if identity.email is not None and identity.email.strip():
        local_part = identity.email.strip().partition("@")[0]
        if local_part:
            return DisplayName(local_part[:MAX_DISPLAY_NAME_LENGTH])
    return OWNER_FALLBACK_NAME if identity.owner else GUEST_FALLBACK_NAME
