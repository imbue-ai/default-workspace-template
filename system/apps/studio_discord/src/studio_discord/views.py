"""Turns what Discord returned into what the window shows. Pure functions; no I/O."""

from typing import Any

from studio_discord import config
from studio_discord.discord import plain_text


def _name(user: dict[str, Any]) -> str:
    return user.get("global_name") or user.get("username") or "Someone"


def avatar_url(user: dict[str, Any]) -> str | None:
    avatar = user.get("avatar")
    return f"https://cdn.discordapp.com/avatars/{user['id']}/{avatar}.png?size=64" if avatar else None


def account(user: dict[str, Any]) -> dict[str, Any]:
    return {"name": _name(user), "avatar": avatar_url(user)}


def message_item(channel_id: str, message: dict[str, Any]) -> dict[str, Any]:
    author = message.get("author", {})
    return {
        "id": message["id"],
        "author": _name(author),
        "avatar": avatar_url(author),
        "time": message.get("timestamp"),
        "title": None,
        "text": plain_text(message),
        "attachments": len(message.get("attachments", [])),
        "replies": None,
        "url": config.message_url(channel_id, message["id"]),
    }


def post_item(thread: dict[str, Any], authors: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A forum post (a thread). The opening message, when Discord sent it, gives the author and text."""
    first = authors.get(thread["id"])
    metadata = thread.get("thread_metadata", {})
    return {
        "id": thread["id"],
        "author": _name(first["author"]) if first else None,
        "avatar": avatar_url(first["author"]) if first else None,
        "time": metadata.get("create_timestamp"),
        "title": thread.get("name") or "Untitled post",
        "text": plain_text(first) if first else "",
        "attachments": len(first.get("attachments", [])) if first else 0,
        "replies": thread.get("message_count"),
        "url": config.channel_url(thread["id"]),
    }


def _sort_key(item: dict[str, Any]) -> str:
    return item.get("time") or ""


def channel_items(channel: config.Channel, kept: dict[str, Any]) -> list[dict[str, Any]]:
    """The newest few records kept for a channel, shaped for the window, newest first."""
    records = list(kept.get("items", {}).values())
    if channel.kind == "forum":
        threads = [r for r in records if r.get("kind") == "thread"]
        openers = {r["channel_id"]: r for r in records if r.get("kind") == "first_message"}
        items = [post_item(r["thread"], {k: v["message"] for k, v in openers.items()}) for r in threads]
    else:
        items = [message_item(channel.id, r) for r in records]
    items.sort(key=_sort_key, reverse=True)
    return items[: config.MESSAGES_PER_CHANNEL]


def channel_view(channel: config.Channel, raw: dict[str, Any] | None, kept: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": channel.id,
        "name": channel.name,
        "kind": channel.kind,
        "topic": (raw or {}).get("topic") or channel.topic,
        "url": config.channel_url(channel.id),
        "items": channel_items(channel, kept),
    }


def widget_view(widget: dict[str, Any] | None) -> dict[str, Any] | None:
    if not widget:
        return None
    members = widget.get("members", [])
    online_first = sorted(members, key=lambda m: m.get("status") != "online")
    return {
        "name": widget.get("name"),
        "online": widget.get("presence_count", len(members)),
        "faces": [
            {"name": m.get("username"), "avatar": m.get("avatar_url"), "status": m.get("status")}
            for m in online_first[:8]
        ],
    }
