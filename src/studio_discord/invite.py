"""The join link: the live one from the template's catalog file, else the server widget's, else the last known."""

import time
from datetime import UTC, datetime
from typing import Any

from studio_discord import config
from studio_discord.web import HttpError, UnreachableError, request_json

_TTL_SECONDS = 600.0
_cache: tuple[float, str] | None = None


def _unexpired(entry: dict[str, Any], now: datetime) -> bool:
    expires = entry.get("invite_expires_at")
    if not expires:
        return True
    try:
        return datetime.fromisoformat(expires) > now
    except ValueError:
        return True


def choose(catalog: dict[str, Any] | None, widget: dict[str, Any] | None, now: datetime) -> str:
    if catalog and catalog.get("invite_url") and _unexpired(catalog, now):
        return str(catalog["invite_url"])
    if widget and widget.get("instant_invite"):
        return str(widget["instant_invite"])
    return config.LAST_KNOWN_INVITE


def join_link(widget: dict[str, Any] | None) -> str:
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < _TTL_SECONDS:
        return _cache[1]
    try:
        catalog = request_json(config.INVITE_CATALOG_URL)
    except (HttpError, UnreachableError, ValueError):
        catalog = None
    link = choose(catalog, widget, datetime.now(UTC))
    _cache = (time.monotonic(), link)
    return link
