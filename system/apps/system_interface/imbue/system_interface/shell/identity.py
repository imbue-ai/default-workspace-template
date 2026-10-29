"""The request identity a proxy vouches for: the ``X-Imbue-Identity`` header every request into the workspace carries
(the share identity spec, section 4.2), parsed once for the routes that key behaviour on who is asking.

The parsing itself is the ``workspace_profile`` library's -- one definition for this shell, the
chat app, and the profile resolution alike. A missing header means the request came through no
current proxy (an older forward, an in-container caller): it is treated as the owner with no
account, never as a visitor; an unreadable one is logged and treated the same way.
"""

from workspace_layout.primitives import UserId
from workspace_profile.identity import ANONYMOUS_OWNER as ANONYMOUS_OWNER
from workspace_profile.identity import IDENTITY_HEADER as IDENTITY_HEADER
from workspace_profile.identity import RequestIdentity as RequestIdentity
from workspace_profile.identity import parse_identity_header as parse_identity_header

from imbue.imbue_common.pure import pure


@pure
def visiting_user_id(identity: RequestIdentity) -> UserId | None:
    """The user id of a signed-in requester other than the owner, the one kind of requester who gets a desktop of
    their own on arrival (desktop plan section 3.10); None for the owner and for a requester with no account."""
    if identity.owner or identity.user_id is None:
        return None
    return UserId(identity.user_id)
