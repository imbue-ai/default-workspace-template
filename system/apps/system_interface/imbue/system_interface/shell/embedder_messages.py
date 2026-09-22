"""The embedder-message relay (desktop contracts.md section 5.6): a message the minds chrome sent the shell's page is
posted, with the client that received it, to every app whose registry row names its type in ``message_handlers``.

The shell reads no payload: the app that registered the type is the one that knows what it means.
"""

import time
from collections.abc import Sequence
from typing import Any
from typing import Final

import httpx
from app_manifest.primitives import AppName
from app_manifest.primitives import MessageType
from app_manifest.registry import RegistryRow
from loguru import logger
from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import ClientId

# A handler is one loopback request to an app that answers once it has asked the shell for what it wants; past the
# first threshold it is suspicious, past the second it is broken.
MESSAGE_DELIVERY_SLOW_SECONDS: Final[float] = 2.0
MESSAGE_DELIVERY_TIMEOUT_SECONDS: Final[float] = 10.0

# The keys the posted body is built around; a payload carrying one would be overwritten, so it is refused.
_RESERVED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset({"type", "client_id"})

# How much of a refusal's body a delivery quotes.
_REFUSAL_DETAIL_LIMIT: Final[int] = 300


class EmbedderMessageRelayRequest(FrozenModel):
    """The body of ``POST /api/embedder-messages``: a message the shell's page received from the minds chrome."""

    type: MessageType = Field(description="The message's type")
    client_id: ClientId = Field(description="The client whose page received the message")
    payload: dict[str, Any] = Field(default_factory=dict, description="The message's fields other than its type")

    @model_validator(mode="after")
    def _check_payload_leaves_the_envelope_alone(self) -> "EmbedderMessageRelayRequest":
        reserved = sorted(_RESERVED_PAYLOAD_KEYS & set(self.payload))
        if reserved:
            raise InvalidShellValueError(f"the payload must not carry {reserved}: the relay sets them")
        return self


class ForwardedMessage(FrozenModel):
    """One post of a relayed message: the app, where it goes, and the body."""

    app: AppName = Field(description="The app whose row registered the message's type")
    url: str = Field(description="The app's registered URL plus the handler's path")
    body: dict[str, Any] = Field(description="The message: its type, the client, and its payload's fields")


class MessageDelivery(FrozenModel):
    """What one app answered a relayed message."""

    app: AppName = Field(description="The app posted to")
    status: int | None = Field(description="The app's HTTP status, or None when it could not be reached")
    detail: str = Field(description="Empty for a 2xx; otherwise why the app did not take the message")
    is_delivered: bool = Field(description="Whether the app answered with a 2xx")


@pure
def forwarded_messages(rows: Sequence[RegistryRow], request: EmbedderMessageRelayRequest) -> list[ForwardedMessage]:
    """The post each app registered for the message's type is owed, in registry order."""
    body = {**request.payload, "type": str(request.type), "client_id": str(request.client_id)}
    return [
        ForwardedMessage(app=row.name, url=f"{str(row.url).rstrip('/')}{handler.path}", body=body)
        for row in rows
        for handler in row.message_handlers
        if handler.type == request.type
    ]


def deliver_forwarded_message(forwarded: ForwardedMessage) -> MessageDelivery:
    """Post one relayed message and report what the app answered; an app that cannot be reached is reported, not
    raised, so one app's failure does not cost the others their message."""
    started_at = time.monotonic()
    try:
        response = httpx.post(forwarded.url, json=forwarded.body, timeout=MESSAGE_DELIVERY_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        logger.warning("Could not post {} to {} at {}: {}", forwarded.body["type"], forwarded.app, forwarded.url, e)
        return MessageDelivery(app=forwarded.app, status=None, detail=f"could not be reached: {e}", is_delivered=False)
    elapsed = time.monotonic() - started_at
    if elapsed > MESSAGE_DELIVERY_SLOW_SECONDS:
        logger.warning("Posted {} to {} slowly, in {:.1f}s", forwarded.body["type"], forwarded.app, elapsed)
    if response.is_success:
        return MessageDelivery(app=forwarded.app, status=response.status_code, detail="", is_delivered=True)
    detail = response.text.strip()[:_REFUSAL_DETAIL_LIMIT] or f"answered {response.status_code}"
    logger.warning(
        "Posted {} to {} and it answered {}: {}", forwarded.body["type"], forwarded.app, response.status_code, detail
    )
    return MessageDelivery(app=forwarded.app, status=response.status_code, detail=detail, is_delivered=False)


@pure
def message_delivery_wire_json(delivery: MessageDelivery) -> dict[str, Any]:
    return {"app": str(delivery.app), "status": delivery.status, "detail": delivery.detail}
