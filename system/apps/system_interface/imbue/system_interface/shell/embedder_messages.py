"""The message relay (desktop contracts.md section 5.6): a message the shell's page received, from the Imbue Studio chrome
or from an app's frame, is delivered to every app whose registry row names its type in ``message_handlers``: posted,
with the client that received it, to a handler's route, or shown as the page a ``show`` handler builds from it.

The shell reads a payload only to fill a ``show`` handler's page: the app that registered the type is the one that
knows what it means.
"""

import time
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

import httpx
from app_manifest.errors import PageTemplateFieldError
from app_manifest.primitives import AppName
from app_manifest.primitives import MessageType
from app_manifest.primitives import PageTemplate
from app_manifest.primitives import render_page_template
from app_manifest.registry import RegistryRow
from loguru import logger
from pydantic import Field
from pydantic import model_validator
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import WindowId

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.primitives import ShowOutcome
from imbue.system_interface.shell.primitives import WindowPath

# A handler is one loopback request to an app that answers once it has asked the shell for what it wants; past the
# first threshold it is suspicious, past the second it is broken.
MESSAGE_DELIVERY_SLOW_SECONDS: Final[float] = 2.0
MESSAGE_DELIVERY_TIMEOUT_SECONDS: Final[float] = 10.0

# The keys the posted body is built around; a payload carrying one would be overwritten, so it is refused.
_RESERVED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset({"type", "client_id"})

# How much of a refusal's body a delivery quotes.
_REFUSAL_DETAIL_LIMIT: Final[int] = 300

# The sender of a message the Imbue Studio chrome sent; any other sender is the app whose frame sent it.
EMBEDDER_SENDER: Final[AppName] = AppName("embedder")


class EmbedderMessageRelayRequest(FrozenModel):
    """The body of ``POST /api/embedder-messages``: a message the shell's page received, from the Imbue Studio chrome
    or from an app's frame."""

    type: MessageType = Field(description="The message's type")
    client_id: ClientId = Field(description="The client whose page received the message")
    payload: dict[str, Any] = Field(default_factory=dict, description="The message's fields other than its type")
    sender: AppName = Field(
        default=EMBEDDER_SENDER,
        description="Who sent the message: ``embedder`` for the Imbue Studio chrome, else the sending app; for logs only",
    )

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


class ShownPage(FrozenModel):
    """One page a ``show`` handler owes a relayed message: the app, and its page and ``showing`` pages built from the
    message, or why they could not be built."""

    app: AppName = Field(description="The app whose row registered the message's type")
    page: WindowPath | None = Field(description="The page to show; None when the message could not fill the template")
    showing: tuple[WindowPath, ...] = Field(description="The app's other pages that count as already showing it")
    refusal: str = Field(description="Empty when the pages were built; otherwise why they could not be")


class MessageDelivery(FrozenModel):
    """What one app's handler did with a relayed message."""

    app: AppName = Field(description="The app the message was delivered to")
    status: int | None = Field(
        description="The app's HTTP status for a posted message, 200 for a page shown, or None when the app could "
        "not be reached or the page not built or shown"
    )
    detail: str = Field(description="Empty when delivered; otherwise why the app did not take the message")
    is_delivered: bool = Field(description="Whether the app took the message")
    shown: ShowOutcome | None = Field(
        default=None, description="For a page shown: which way the ``show`` op went (raised, navigated, ...)"
    )
    window_id: WindowId | None = Field(default=None, description="For a page shown: the window that shows it")


@pure
def forwarded_messages(rows: Sequence[RegistryRow], request: EmbedderMessageRelayRequest) -> list[ForwardedMessage]:
    """The post each ``path`` handler registered for the message's type is owed, in registry order."""
    body = {**request.payload, "type": str(request.type), "client_id": str(request.client_id)}
    return [
        ForwardedMessage(app=row.name, url=f"{str(row.url).rstrip('/')}{handler.path}", body=body)
        for row in rows
        for handler in row.message_handlers
        if handler.type == request.type and handler.path is not None
    ]


@pure
def shown_pages(rows: Sequence[RegistryRow], request: EmbedderMessageRelayRequest) -> list[ShownPage]:
    """The page each ``show`` handler registered for the message's type builds from its payload, in registry order;
    a payload that cannot fill a handler's templates is that handler's refusal, not the others'."""
    pages: list[ShownPage] = []
    for row in rows:
        for handler in row.message_handlers:
            if handler.type != request.type or handler.show is None:
                continue
            try:
                page, showing = _built_pages(handler.show, handler.showing, request.payload)
            except (PageTemplateFieldError, InvalidShellValueError) as e:
                pages.append(ShownPage(app=row.name, page=None, showing=(), refusal=str(e)))
                continue
            pages.append(ShownPage(app=row.name, page=page, showing=showing, refusal=""))
    return pages


@pure
def _built_pages(
    show: PageTemplate, showing: Sequence[PageTemplate], payload: Mapping[str, Any]
) -> tuple[WindowPath, tuple[WindowPath, ...]]:
    page = WindowPath(render_page_template(show, payload))
    return page, tuple(WindowPath(render_page_template(template, payload)) for template in showing)


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
    detail = _refusal_detail(response) or f"answered {response.status_code}"
    logger.warning(
        "Posted {} to {} and it answered {}: {}", forwarded.body["type"], forwarded.app, response.status_code, detail
    )
    return MessageDelivery(app=forwarded.app, status=response.status_code, detail=detail, is_delivered=False)


def _refusal_detail(response: httpx.Response) -> str:
    """Why an app refused a message, in its own words: the ``detail`` of a JSON answer (as a launch path's refusal
    carries it), else the answer's text."""
    try:
        body = response.json()
    except ValueError as e:
        logger.debug("A refusal from {} was not JSON ({}); quoting its text", response.url, e)
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"].strip()[:_REFUSAL_DETAIL_LIMIT]
    return response.text.strip()[:_REFUSAL_DETAIL_LIMIT]


@pure
def message_delivery_wire_json(delivery: MessageDelivery) -> dict[str, Any]:
    wire: dict[str, Any] = {"app": str(delivery.app), "status": delivery.status, "detail": delivery.detail}
    if delivery.shown is not None:
        wire["shown"] = delivery.shown.value
    if delivery.window_id is not None:
        wire["window_id"] = str(delivery.window_id)
    return wire
