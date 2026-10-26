"""Rename the chat under one title to another through the chat app.

For an app whose ``display_name`` changed: every window's "Chat about this app" opens the chat
titled ``<display_name> Chat`` (the multiplayer spec, section 9), so the chat under the old
title is renamed to the new one, or the button would start a second chat (the ``build-app``
skill, "Renaming an app"). The chat app is reached as ``message_chat.py`` reaches it (its
registry row, else the fixed fallback), the chats listed from ``GET /api/chats``, and the one
titled exactly ``--title`` renamed with ``POST /api/chats/<id>/rename``.

Exit codes:
    0  -- renamed, or no chat carries the old title (nothing to do)
    1  -- the chat app refused the rename (the reason is printed)
    2  -- the chat app could not be reached or answered no listing

Standard-library only, like ``message_chat.py`` beside it (whose chat-app helpers it imports, as
``seed_welcome_chat.py`` does), so it runs under a plain ``python3``.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path
from typing import IO

from message_chat import ChatAppUnreachableError, chat_app_url, get_json, post_json

CHATS_PATH = "/api/chats"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_UNREACHABLE = 2


def chats_listed(listing: object) -> list[object] | None:
    """The chats of a ``GET /api/chats`` answer, or None for a body of another shape (no listing at all)."""
    if not isinstance(listing, dict) or not isinstance(listing.get("chats"), list):
        return None
    return listing["chats"]


def chat_id_titled(chats: list[object], title: str) -> str | None:
    """The id of the listed chat titled exactly ``title``, or None."""
    for chat in chats:
        if (
            isinstance(chat, dict)
            and chat.get("title") == title
            and isinstance(chat.get("chat_id"), str)
        ):
            return chat["chat_id"]
    return None


def rename_chat(
    base_url: str, title: str, new_title: str, stdout: IO[str], stderr: IO[str]
) -> int:
    try:
        listed = get_json(base_url, CHATS_PATH)
    except ChatAppUnreachableError as exc:
        print(str(exc), file=stderr)
        return EXIT_UNREACHABLE
    if listed.status != 200:
        print(
            f"the chat app answered HTTP {listed.status} to the chat listing",
            file=stderr,
        )
        return EXIT_UNREACHABLE
    chats = chats_listed(listed.body)
    if chats is None:
        print("the chat app answered no listing", file=stderr)
        return EXIT_UNREACHABLE
    chat_id = chat_id_titled(chats, title)
    if chat_id is None:
        print(f"no chat is titled {title!r}; nothing to rename", file=stdout)
        return EXIT_OK
    try:
        answer = post_json(
            base_url,
            f"/api/chats/{urllib.parse.quote(chat_id, safe='')}/rename",
            {"title": new_title},
        )
    except ChatAppUnreachableError as exc:
        print(str(exc), file=stderr)
        return EXIT_UNREACHABLE
    if answer.status != 200:
        print(f"the chat app refused the rename: {answer.detail}", file=stderr)
        return EXIT_REFUSED
    print(f"renamed {title!r} ({chat_id}) to {new_title!r}", file=stdout)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rename the chat under one title to another, for an app whose display name changed."
    )
    parser.add_argument(
        "--title", required=True, help="The exact title of the chat to rename"
    )
    parser.add_argument("--new-title", required=True, help="The title to give it")
    return parser


def main(
    argv: list[str] | None = None,
    stdout: IO[str] = sys.stdout,
    stderr: IO[str] = sys.stderr,
) -> int:
    args = _build_parser().parse_args(argv)
    base_url = chat_app_url(os.environ, Path.cwd())
    return rename_chat(base_url, args.title, args.new_title, stdout, stderr)


if __name__ == "__main__":
    sys.exit(main())
