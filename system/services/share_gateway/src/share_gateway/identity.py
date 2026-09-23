"""The requester identity a verified request carries into the workspace.

One record describes whoever is asking: the owner flag always, and -- because a
session only exists for a signed-in visitor or owner -- the account's user id
and verified email. The gateway renders it as the single ``X-Imbue-Identity``
header caddy copies onto every request the services see (the same header the
local forward stamps, so a service reads request identity identically on either
path). Profile data (display name, profile picture) is not part of the
record: it lives in the connector, and a service that needs it looks it up by
``user_id``.
"""

import json

IDENTITY_HEADER = "X-Imbue-Identity"


class RequesterIdentity:
    """Who is making a request: the owner flag plus the signed-in account's user id and verified email."""

    def __init__(self, user_id: str, email: str, is_owner: bool) -> None:
        self.user_id = user_id
        self.email = email
        self.is_owner = is_owner

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RequesterIdentity):
            return NotImplemented
        return vars(self) == vars(other)

    def __repr__(self) -> str:
        return f"RequesterIdentity({vars(self)!r})"


def render_identity_header(identity: RequesterIdentity) -> str:
    """The compact JSON value of ``X-Imbue-Identity`` for a verified requester."""
    document: dict[str, object] = {
        "owner": identity.is_owner,
        "user_id": identity.user_id,
        "email": identity.email,
    }
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True)
