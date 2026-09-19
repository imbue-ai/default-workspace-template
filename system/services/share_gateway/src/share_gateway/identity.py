"""The requester identity a verified request carries into the workspace.

One record describes whoever is asking: the owner flag always, and -- because a
session only exists for a signed-in visitor or owner -- the account's user id,
verified email, and optional display name and avatar. The gateway renders it as
the single ``X-Imbue-Identity`` header caddy copies onto every request the
services see (the same header the local forward stamps, so a service reads
request identity identically on either path). ``display_name`` and
``avatar_url`` are omitted from the header when the account has none.
"""

import json

IDENTITY_HEADER = "X-Imbue-Identity"


class RequesterIdentity:
    """Who is making a request: the owner flag plus the signed-in account's record."""

    def __init__(
        self,
        user_id: str,
        email: str,
        is_owner: bool,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.email = email
        self.is_owner = is_owner
        self.display_name = display_name
        self.avatar_url = avatar_url

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
    if identity.display_name:
        document["display_name"] = identity.display_name
    if identity.avatar_url:
        document["avatar_url"] = identity.avatar_url
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True)
