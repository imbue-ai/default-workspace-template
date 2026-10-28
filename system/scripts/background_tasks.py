#!/usr/bin/env python3
"""The background tasks chats are waiting on, and whether a chat is busy.

    python3 system/scripts/background_tasks.py list [--chat <chat-id>] [--format json|tsv]
    python3 system/scripts/background_tasks.py is-busy <chat-id>    # exit 0 busy, 1 not

``list`` prints every chat with pending tasks; ``tsv`` is one line per chat,
``chat_id<TAB>active_agent_id<TAB>active_agent_name<TAB>is_busy<TAB>task_count``. Both ask the
chat app first and read the marker files only when it cannot answer; ``is_busy`` from the chat
app also counts a turn in flight, which the files cannot see.

The markers and their readers are ``system/libs/chat_background_tasks``, which this script puts
on ``sys.path``: skills run it with a bare ``python3``.
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "libs" / "chat_background_tasks" / "src"),
)

import message_chat
from chat_background_tasks.listing import (
    AGENTS_PATH,
    ChatAppUnavailableError,
    ChatTasks,
    chats_from_agents_payload,
    chats_from_files,
    get_json,
)
from chat_background_tasks.markers import marker_root

EXIT_BUSY = 0
EXIT_NOT_BUSY = 1


def known_chats(environ: Mapping[str, str], cwd: Path) -> tuple[list[ChatTasks], str]:
    """The chats the chat app reports, or those the files hold live tasks for, and which answered
    (``chat_app`` or ``files``)."""
    base_url = message_chat.chat_app_url(environ, cwd)
    try:
        return chats_from_agents_payload(get_json(base_url, AGENTS_PATH)), "chat_app"
    except ChatAppUnavailableError as exc:
        print(f"background_tasks.py: reading the marker files ({exc})", file=sys.stderr)
        return chats_from_files(marker_root(environ)), "files"


def cmd_list(
    chat_filter: str | None, output_format: str, environ: Mapping[str, str], cwd: Path
) -> int:
    chats, source = known_chats(environ, cwd)
    chats = [
        chat for chat in chats if chat.tasks and chat_filter in (None, chat.chat_id)
    ]
    if output_format == "tsv":
        for chat in chats:
            print(chat.to_tsv())
    else:
        print(
            json.dumps(
                {"source": source, "chats": [chat.to_json() for chat in chats]},
                indent=2,
            )
        )
    return 0


def cmd_is_busy(chat_id: str, environ: Mapping[str, str], cwd: Path) -> int:
    chats, _ = known_chats(environ, cwd)
    return (
        EXIT_BUSY
        if any(chat.chat_id == chat_id and chat.is_busy for chat in chats)
        else EXIT_NOT_BUSY
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The background tasks chats are waiting on, and whether a chat is busy."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    list_parser = commands.add_parser(
        "list", help="Print every chat with pending background tasks."
    )
    list_parser.add_argument("--chat", default=None, help="Only this chat id.")
    list_parser.add_argument("--format", choices=("json", "tsv"), default="json")
    is_busy_parser = commands.add_parser(
        "is-busy", help="Exit 0 when the chat is busy, 1 when it is not."
    )
    is_busy_parser.add_argument("chat_id")
    return parser


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    args = _build_parser().parse_args(argv)
    resolved_environ = os.environ if environ is None else environ
    if args.command == "list":
        return cmd_list(args.chat, args.format, resolved_environ, Path.cwd())
    return cmd_is_busy(args.chat_id, resolved_environ, Path.cwd())


if __name__ == "__main__":
    raise SystemExit(main())
