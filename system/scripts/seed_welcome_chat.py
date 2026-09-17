#!/usr/bin/env python3
"""Open the workspace's welcome chat on the conversation the Mind app had before the workspace existed.

Usage, from the repo root (the cwd ``mngr exec`` gives every command there)::

    python3 system/scripts/seed_welcome_chat.py --transcript-base64 <base64 of the JSON body>
    python3 system/scripts/seed_welcome_chat.py --transcript-file path/to/body.json

The body is what the chat app's ``POST /api/chats/seed`` takes: ``{"title": "...", "turns":
[{"role": "user" | "assistant", "text": "..."}, ...]}``. The Mind app runs this through ``mngr
exec`` the moment a workspace is ready, so the onboarding conversation continues inside the
workspace as its first chat; the transcript rides the command line (base64, since it holds
newlines and quotes) because ``mngr exec`` carries no stdin.

On success the script prints one JSON line, ``{"chat_id": "agent-..."}``, and exits 0. The
chat app answers 503 until it has read its agent list from mngr, and it may not be listening at
all in the first seconds after the workspace comes up, so both are retried for a bounded window
before they count as a failure (exit 1).

Standard library only, like every script here: the chat app is found through its row in the
app registry (``data/.state/apps.toml``, or ``$MINDS_APPS_FILE``), with the chat app's fixed
port as the fallback, through the helpers ``message_chat.py`` beside this script keeps.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from message_chat import ChatAppUnreachableError, chat_app_url, post_json

SEED_PATH = "/api/chats/seed"

EXIT_SEEDED = 0
EXIT_FAILED = 1

# How long the chat app is retried while it is not listening yet or not ready (503): it starts
# beside the shell the Mind app's readiness probe waits for, and reads its agent list a few
# seconds after that.
RETRY_WINDOW_SECONDS = 120.0
RETRY_INTERVAL_SECONDS = 1.0


class SeedFailedError(Exception):
    """The chat app refused the seed, or could not be reached within the window."""


def seed_through_chat_app(
    base_url: str,
    body: dict[str, object],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> str:
    """POST the seed, retrying an unreachable or not-ready chat app for the window; the chat id, or a raised failure."""
    started_at = clock()
    while True:
        try:
            answer = post_json(base_url, SEED_PATH, body)
        except ChatAppUnreachableError as exc:
            detail = str(exc)
            answer = None
        except (OSError, http.client.HTTPException) as exc:
            raise SeedFailedError(f"the chat app dropped the request: {exc}") from exc
        if answer is not None:
            if 200 <= answer.status < 300:
                chat_id = (
                    answer.body.get("chat_id")
                    if isinstance(answer.body, dict)
                    else None
                )
                if not isinstance(chat_id, str) or not chat_id:
                    raise SeedFailedError("the chat app answered without a chat id")
                return chat_id
            if answer.status != 503:
                raise SeedFailedError(answer.detail)
            detail = answer.detail
        if clock() - started_at >= RETRY_WINDOW_SECONDS:
            raise SeedFailedError(detail)
        sleep(RETRY_INTERVAL_SECONDS)


def _read_body(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> dict[str, object]:
    if args.transcript_base64 is not None:
        raw = base64.b64decode(args.transcript_base64).decode("utf-8")
    else:
        raw = Path(args.transcript_file).read_text(encoding="utf-8")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        parser.error(f"the transcript is not valid JSON: {exc}")
    if not isinstance(body, dict):
        parser.error("the transcript must be a JSON object with a title and turns")
    return body


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open the workspace's welcome chat on a conversation the Mind app had before the workspace existed.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--transcript-base64",
        help="The seed body (title and turns) as base64-encoded JSON.",
    )
    source.add_argument(
        "--transcript-file", help="A file holding the seed body as JSON."
    )
    return parser


def main(
    argv: list[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    body = _read_body(parser, args)
    base_url = chat_app_url(os.environ, Path.cwd())
    try:
        chat_id = seed_through_chat_app(base_url, body, clock, sleep)
    except SeedFailedError as exc:
        print(f"Could not seed the welcome chat: {exc}", file=sys.stderr)
        return EXIT_FAILED
    print(json.dumps({"chat_id": chat_id}))
    return EXIT_SEEDED


if __name__ == "__main__":
    raise SystemExit(main())
