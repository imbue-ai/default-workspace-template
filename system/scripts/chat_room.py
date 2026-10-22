#!/usr/bin/env python3
"""Read a chat room, or post into one as the workspace's agent, through the chat app (the multiplayer spec,
section 7.4).

Usage, from the repo root (every skill's cwd)::

    python3 system/scripts/chat_room.py read <room> [--since <event_id>] [--limit N]
    python3 system/scripts/chat_room.py post <room> -m "text"
    some-command | python3 system/scripts/chat_room.py post <room>

A room is named by its id (``room-...``, or the id of a chat that became a room) or by its title,
case-insensitively; the Lounge is made on first use. ``read`` prints the room's messages as
``[time] <name>: text`` lines, mentions intact, newest last; ``--since`` prints only those after
the event id the line ends with (``[time] <name>: text  (event_id)``), ``--limit`` the last N.
``post`` appends the text as the workspace agent, named after the workspace: that is the only way
an agent's words reach a room, and a reply posted into a room is the notification, so the
notify-user script is not run for it. Exit 0 on success, 1 when the room is unknown or the chat
app refused or could not be reached.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from message_chat import (  # noqa: E402
    ChatAppUnreachableError,
    chat_app_url,
    post_json,
    request_json,
)

LOUNGE_TITLE = "Lounge"
EXIT_OK = 0
EXIT_FAILED = 1
WORKSPACE_AGENT_AUTHOR = "workspace-agent"


def _print_error(text: str) -> None:
    print(f"chat_room: {text}", file=sys.stderr)


def resolve_room(base_url: str, room_ref: str) -> dict[str, Any] | None:
    """The room ``room_ref`` names, by id or by title (case-insensitively); the Lounge is made when asked for."""
    answer = request_json(base_url, "GET", "/api/rooms")
    if answer.status != 200 or not isinstance(answer.body, dict):
        _print_error(f"the chat app answered {answer.status} to the room list")
        return None
    rooms = answer.body.get("rooms", [])
    wanted = room_ref.strip().casefold()
    for room in rooms:
        if isinstance(room, dict) and (
            room.get("id") == room_ref
            or str(room.get("title", "")).casefold() == wanted
        ):
            return room
    if wanted == LOUNGE_TITLE.casefold():
        made = post_json(base_url, "/api/rooms/lounge", {})
        if made.status == 200 and isinstance(made.body, dict):
            return made.body
        _print_error(f"the chat app answered {made.status} to making the Lounge")
        return None
    _print_error(f"no room is called or has the id {room_ref!r}")
    return None


def format_message(message: Mapping[str, Any]) -> str:
    """One message as a line: ``[time] <name>: text  (event_id)``, the text's own line breaks indented under it."""
    text = str(message.get("content", "")).replace("\n", "\n    ")
    return f"[{message.get('timestamp', '')}] {message.get('author_name', '')}: {text}  ({message.get('event_id', '')})"


def read_room(
    base_url: str,
    room: Mapping[str, Any],
    since: str | None,
    limit: int | None,
    out: IO[str],
) -> int:
    query = f"?after={urllib.parse.quote(since)}" if since else ""
    answer = request_json(
        base_url,
        "GET",
        f"/api/rooms/{urllib.parse.quote(str(room['id']))}/messages{query}",
    )
    if answer.status != 200 or not isinstance(answer.body, dict):
        _print_error(f"the chat app answered {answer.status} to reading the room")
        return EXIT_FAILED
    messages = [
        message
        for message in answer.body.get("events", [])
        if isinstance(message, dict)
    ]
    if limit is not None:
        messages = messages[-limit:] if limit > 0 else []
    print(
        f"# {room.get('title', '')} ({room.get('id', '')}): {len(messages)} message(s)",
        file=out,
    )
    for message in messages:
        print(format_message(message), file=out)
    return EXIT_OK


def post_to_room(base_url: str, room: Mapping[str, Any], text: str) -> int:
    answer = post_json(
        base_url,
        f"/api/rooms/{urllib.parse.quote(str(room['id']))}/post",
        {"message": text, "author": WORKSPACE_AGENT_AUTHOR},
    )
    if answer.status != 200:
        detail = (
            answer.body.get("detail") if isinstance(answer.body, dict) else answer.body
        )
        _print_error(f"the chat app answered {answer.status} to the post: {detail}")
        return EXIT_FAILED
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a chat room, or post into one as the workspace's agent."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read", help="print the room's messages")
    read.add_argument("room", help="the room's id or title")
    read.add_argument(
        "--since", default=None, help="print only the messages after this event id"
    )
    read.add_argument(
        "--limit", type=int, default=None, help="print only the last N messages"
    )
    post = commands.add_parser("post", help="post into the room as the workspace agent")
    post.add_argument("room", help="the room's id or title")
    post.add_argument(
        "-m", "--message", default=None, help="the text; read from stdin when absent"
    )
    return parser


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    out: IO[str] | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    resolved_environ = os.environ if environ is None else environ
    base_url = chat_app_url(resolved_environ, Path.cwd())
    try:
        room = resolve_room(base_url, args.room)
        if room is None:
            return EXIT_FAILED
        if args.command == "read":
            return read_room(
                base_url,
                room,
                args.since,
                args.limit,
                sys.stdout if out is None else out,
            )
        text = (
            args.message
            if args.message is not None
            else (sys.stdin if stdin is None else stdin).read()
        )
        if not text.strip():
            parser.error("the message is empty")
        return post_to_room(base_url, room, text.strip("\n"))
    except ChatAppUnreachableError as exc:
        _print_error(str(exc))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
