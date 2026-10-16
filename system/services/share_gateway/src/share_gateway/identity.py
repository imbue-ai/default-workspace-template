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

BEARER_PREFIX = "Bearer "

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


class AgentIdentity:
    """A visiting agent making a request: never the owner, never a human account; named by its grant."""

    def __init__(self, agent_id: str, agent_name: str, granter: str) -> None:
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.granter = granter

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentIdentity):
            return NotImplemented
        return vars(self) == vars(other)

    def __repr__(self) -> str:
        return f"AgentIdentity({vars(self)!r})"


def render_agent_identity_header(identity: AgentIdentity) -> str:
    """The ``X-Imbue-Identity`` value for a visiting agent: ``owner`` false, no ``user_id``, the agent's own fields.

    A consumer that keys on ``user_id`` finds none and must read ``agent_id``
    rather than treating the requester as an anonymous person.
    """
    document: dict[str, object] = {
        "owner": False,
        "agent_id": identity.agent_id,
        "agent_name": identity.agent_name,
        "granter": identity.granter,
    }
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True)


def bearer_token_from_headers(authorization: str | None) -> str | None:
    """The admission token a request presents, or None when the header is absent or not a bearer."""
    if authorization is None or not authorization.startswith(BEARER_PREFIX):
        return None
    token = authorization[len(BEARER_PREFIX) :].strip()
    return token or None
