"""Reads from Discord: the public widget directly, everything else through the workspace's latchkey gateway.

Every signed-in read goes through ``_get``, which refuses any path that is not about one of the hardcoded Studio
channels or the signed-in user. That is what keeps the app inside the Studio channels even though Discord's grant
covers the whole account.
"""

import os
import re
import time
from typing import Any

from studio_discord import config
from studio_discord.web import HttpError, request_json

API = "https://discord.com/api/v10"
WIDGET_URL = f"https://discord.com/api/guilds/{config.GUILD_ID}/widget.json"

# Discord's "Missing Access": the signed-in user is not in the server (or cannot see the channel).
MISSING_ACCESS = 50001


class OutOfScopeError(Exception):
    """A read this app must never make."""


def _allowed(path: str) -> bool:
    if path == "users/@me":
        return True
    match = re.fullmatch(r"channels/(\d+)(/messages|/threads/search)?", path.split("?", 1)[0])
    return match is not None and match.group(1) in config.CHANNEL_IDS


def _gateway_headers() -> dict[str, str]:
    headers = {"X-Latchkey-Gateway-Password": os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")}
    override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE")
    if override:
        headers["X-Latchkey-Gateway-Permissions-Override"] = override
    return headers


def _get(path: str) -> Any:
    if not _allowed(path):
        raise OutOfScopeError(path)
    gateway = os.environ.get("LATCHKEY_GATEWAY", "").rstrip("/")
    return request_json(f"{gateway}/gateway/{API}/{path}", headers=_gateway_headers())


def me() -> dict[str, Any]:
    return _get("users/@me")


def channel(channel_id: str) -> dict[str, Any]:
    return _get(f"channels/{channel_id}")


def latest_messages(channel_id: str, limit: int) -> list[dict[str, Any]]:
    return _get(f"channels/{channel_id}/messages?limit={limit}")


def latest_posts(forum_id: str, limit: int) -> dict[str, Any]:
    """A forum's most recently active posts (threads), open and archived."""
    return _get(
        f"channels/{forum_id}/threads/search?archived=true&sort_by=last_message_time&sort_order=desc&limit={limit}"
    )


def is_not_a_member(error: HttpError) -> bool:
    return error.status == 403 and error.json_code() == MISSING_ACCESS


_widget_cache: tuple[float, dict[str, Any]] | None = None
_WIDGET_TTL_SECONDS = 60.0


def widget() -> dict[str, Any]:
    """The server's public widget: name, online count, a sample of who is online, its own invite. No sign-in."""
    global _widget_cache
    now = time.monotonic()
    if _widget_cache is not None and now - _widget_cache[0] < _WIDGET_TTL_SECONDS:
        return _widget_cache[1]
    data = request_json(WIDGET_URL)
    _widget_cache = (now, data)
    return data


def plain_text(message: dict[str, Any]) -> str:
    """A message's text with Discord's markup for mentions, channels, and custom emoji made readable."""
    text = message.get("content") or ""
    names = {m["id"]: (m.get("global_name") or m.get("username") or "someone") for m in message.get("mentions", [])}
    text = re.sub(r"<@!?(\d+)>", lambda m: "@" + names.get(m.group(1), "someone"), text)
    known = {c.id: c.name for c in config.CHANNELS}
    text = re.sub(r"<#(\d+)>", lambda m: "#" + known.get(m.group(1), "channel"), text)
    text = re.sub(r"<@&\d+>", "@role", text)
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
    return text.strip()
