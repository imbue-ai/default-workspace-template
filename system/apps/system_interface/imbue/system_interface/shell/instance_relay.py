"""The relay of contracts.md section 6: browsers never reach an app's instances API, the shell forwards every instance verb."""

from typing import Final

import httpx
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.data_types import AppInventoryEntry

INSTANCES_PATH: Final[str] = "/_instances"
RELAY_TIMEOUT_SECONDS: Final[float] = 30.0
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503
JSON_CONTENT_TYPE: Final[str] = "application/json"


class RelayOutcome(FrozenModel):
    """What the app answered, passed through status and body."""

    status_code: int = Field(description="The app's status, or 503 when it could not be reached")
    body: bytes = Field(description="The app's body verbatim")
    content_type: str = Field(description="The app's content type")


def _instances_base(entry: AppInventoryEntry) -> str:
    row = entry.row
    base = str(row.instances_url) if row.instances_url is not None else str(row.url)
    return f"{base.rstrip('/')}{INSTANCES_PATH}"


def _unreachable(entry: AppInventoryEntry, error: Exception) -> RelayOutcome:
    logger.warning("Could not reach the instances API of {}: {}", entry.row.name, error)
    detail = f'{{"detail": "The app {entry.row.name} is unreachable"}}'
    return RelayOutcome(status_code=HTTP_SERVICE_UNAVAILABLE, body=detail.encode(), content_type=JSON_CONTENT_TYPE)


def _relay(client: httpx.Client, entry: AppInventoryEntry, method: str, url: str, body: bytes | None) -> RelayOutcome:
    try:
        response = client.request(
            method,
            url,
            content=body,
            headers={"Content-Type": JSON_CONTENT_TYPE} if body is not None else {},
            timeout=RELAY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        return _unreachable(entry, e)
    return RelayOutcome(
        status_code=response.status_code,
        body=response.content,
        content_type=response.headers.get("content-type", JSON_CONTENT_TYPE),
    )


def relay_create(client: httpx.Client, entry: AppInventoryEntry, body: bytes) -> RelayOutcome:
    return _relay(client, entry, "POST", _instances_base(entry), body)


def relay_delete(client: httpx.Client, entry: AppInventoryEntry, key: str) -> RelayOutcome:
    return _relay(client, entry, "DELETE", f"{_instances_base(entry)}/{key}", None)


def relay_rename(client: httpx.Client, entry: AppInventoryEntry, key: str, body: bytes) -> RelayOutcome:
    return _relay(client, entry, "POST", f"{_instances_base(entry)}/{key}/rename", body)


def relay_location(client: httpx.Client, entry: AppInventoryEntry, key: str, body: bytes) -> RelayOutcome:
    return _relay(client, entry, "POST", f"{_instances_base(entry)}/{key}/location", body)
