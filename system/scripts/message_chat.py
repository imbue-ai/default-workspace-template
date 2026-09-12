#!/usr/bin/env python3
"""Send a message to a chat, by chat id, through the chat app.

Usage, from the repo root (every skill's cwd)::

    python3 system/scripts/message_chat.py <chat-id> -m "text"
    python3 system/scripts/message_chat.py <chat-id> --message-file path/to/task.md
    some-command | python3 system/scripts/message_chat.py <chat-id>
    python3 system/scripts/message_chat.py <chat-id> --system -m "an automated nudge"

This is the in-workspace replacement for ``mngr message <agent>``. A chat is
addressed by its chat id (today the id of its mngr agent, ``$MNGR_AGENT_ID``),
never by its mngr name: a rename changes the name mid-task, and once a chat can
hand off between agents (``docs/system/blueprint/chat-agent-split/``) the chat
app is the only thing that knows which agent is currently taking its messages.
The message is POSTed to the chat app's own send route on loopback, and the
route's verdict is the script's exit status, in ``mngr message``'s vocabulary:

    0  delivered or queued
    1  refused, or the chat app could not be reached and the backoff failed too
    7  delivered, but the agent's input is blocked on a dialog (``mngr
       message``'s "delivered but blocked" code)

``mngr message`` is used only as a BACKOFF, when the chat app cannot take the
message at all: the connection to it fails, or its route keeps answering 404
(an older chat app without the route, or an agent it does not know; a
just-created agent is unknown for a moment after its create, so a 404 is retried
briefly first). Any other answer is the chat app's decision and is never
second-guessed by pasting the text around it: a refusal during a handoff is
what keeps the message from landing on the wrong agent, and a blocked send has
already put the text in the pane.

A 503 means the chat app is up but not ready (it has not read its agent list
from mngr yet, or the agent's daemon is still starting), so it is retried for a
bounded window before it counts as a failure.

Once the chat app has accepted the request, the script waits for its answer with
no read timeout: the route blocks for as long as the harness takes to accept the
text, and giving up part-way would be the one way to deliver the message twice.
The connect timeout is short so an unreachable chat app is detected fast.

``--system`` wraps the text in the sentinel the chat transcript renders as a
collapsed system chip instead of a user bubble (the browser app's wake-up
nudges use it); the tag is pinned against the chat app's copy by a test there.

Standard library only: skills run this as ``python3 system/scripts/...`` and
cron runs it before any venv exists. The chat app is found through its row in
the app registry (``data/.state/apps.toml``, or ``$MINDS_APPS_FILE``), with the
chat app's fixed port as the fallback.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import IO

DEFAULT_APPS_FILE = "data/.state/apps.toml"
ENV_APPS_FILE = "MINDS_APPS_FILE"
CHAT_APP_NAME = "chat"
CHAT_APP_FALLBACK_URL = "http://127.0.0.1:8010"

# Mirrors ``BROWSER_FLEET_TAG`` in the chat app's ``harnesses/message_display.py``
# (and the frontend's copy); a test in the chat app pins the two equal.
SYSTEM_MESSAGE_TAG = "agentic-browser-fleet"

# ``mngr message``'s exit codes (``imbue/mngr/cli/exit_codes.py``).
EXIT_DELIVERED = 0
EXIT_FAILED = 1
EXIT_DELIVERED_BUT_BLOCKED = 7
EXIT_USAGE = 2

# The route's ``kind`` for a send that landed behind a dialog
# (``SendFailureKind.INPUT_BLOCKED`` in mngr).
INPUT_BLOCKED_KIND = "input_blocked"

CONNECT_TIMEOUT_SECONDS = 3.0
# How long a 503 (chat app up, not ready) is retried before it is a failure. The
# route's own revive-and-retry budget for a starting daemon is 15 seconds.
NOT_READY_RETRY_WINDOW_SECONDS = 30.0
NOT_READY_RETRY_INTERVAL_SECONDS = 1.0
# How long a 404 is retried before it means the chat app does not have the chat
# and the backoff takes over. The observe stream reports a new agent within a
# second or two of its create.
UNKNOWN_RETRY_WINDOW_SECONDS = 5.0
UNKNOWN_RETRY_INTERVAL_SECONDS = 0.5


class Outcome(Enum):
    """How a send through the chat app ended."""

    DELIVERED = "delivered"
    BLOCKED = "blocked"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"
    UNKNOWN_CHAT = "unknown_chat"


class ChatAppAnswer:
    """One HTTP answer from the send route."""

    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body

    @property
    def detail(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("detail"), str):
            return self.body["detail"]
        return f"the chat app answered HTTP {self.status}"

    @property
    def kind(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("kind"), str):
            return self.body["kind"]
        return ""


class SendResult:
    """The outcome of the chat-app attempt plus the text to tell the caller."""

    def __init__(self, outcome: Outcome, detail: str) -> None:
        self.outcome = outcome
        self.detail = detail


def wrap_system_message(text: str) -> str:
    """Wrap an automated nudge in the sentinel; adds no newlines, so the wrapped text types into a pane like the bare text."""
    return f"<{SYSTEM_MESSAGE_TAG}>{text}</{SYSTEM_MESSAGE_TAG}>"


def chat_app_url(environ: Mapping[str, str], cwd: Path) -> str:
    """The chat app's base URL: its registry row, else the fixed fallback.

    A missing, unreadable, or rowless registry reads as "not registered yet", never as an
    error: the fallback is the port the chat app has always used.
    """
    apps_file = Path(environ.get(ENV_APPS_FILE) or DEFAULT_APPS_FILE)
    if not apps_file.is_absolute():
        apps_file = cwd / apps_file
    try:
        rows = tomllib.loads(apps_file.read_text(encoding="utf-8")).get("apps", [])
    except (OSError, tomllib.TOMLDecodeError):
        return CHAT_APP_FALLBACK_URL
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("name") == CHAT_APP_NAME
            and isinstance(row.get("url"), str)
            and row["url"]
        ):
            return row["url"].rstrip("/")
    return CHAT_APP_FALLBACK_URL


def _post_json(base_url: str, path: str, body: Mapping[str, str]) -> ChatAppAnswer:
    """POST ``body`` and wait for the answer, however long it takes.

    Raises ``ConnectionError`` (a subclass of ``OSError``) only when the connection itself
    cannot be made; anything after the connect is either an answer or a plain ``OSError``,
    which the caller treats as a failure rather than a reason to back off, because the
    request may have been acted on.
    """
    parsed = urllib.parse.urlsplit(base_url)
    connection = http.client.HTTPConnection(
        parsed.hostname or "127.0.0.1",
        parsed.port or 80,
        timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        connection.connect()
    except OSError as exc:
        raise ConnectionError(
            f"could not connect to the chat app at {base_url}: {exc}"
        ) from exc
    try:
        # The connect timeout has done its job; the send itself blocks for as long as the
        # harness takes to accept the text.
        if connection.sock is not None:
            connection.sock.settimeout(None)
        payload = json.dumps(body).encode("utf-8")
        connection.request(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        parsed_body: object = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        parsed_body = None
    return ChatAppAnswer(response.status, parsed_body)


def send_through_chat_app(
    base_url: str,
    chat_id: str,
    text: str,
    message_id: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> SendResult:
    """Post the message to the chat app's send route, retrying the not-ready answers, and report how it ended."""
    path = f"/api/agents/{urllib.parse.quote(chat_id, safe='')}/message"
    body = {"message": text, "message_id": message_id}
    started_at = clock()
    unknown_since: float | None = None
    while True:
        try:
            answer = _post_json(base_url, path, body)
        except ConnectionError as exc:
            return SendResult(Outcome.UNREACHABLE, str(exc))
        except OSError as exc:
            return SendResult(
                Outcome.REFUSED, f"the chat app dropped the request: {exc}"
            )
        if 200 <= answer.status < 300:
            return SendResult(Outcome.DELIVERED, "")
        if answer.status == 503:
            if clock() - started_at >= NOT_READY_RETRY_WINDOW_SECONDS:
                return SendResult(Outcome.REFUSED, answer.detail)
            sleep(NOT_READY_RETRY_INTERVAL_SECONDS)
            continue
        if answer.status == 404:
            if unknown_since is None:
                unknown_since = clock()
            if clock() - unknown_since >= UNKNOWN_RETRY_WINDOW_SECONDS:
                return SendResult(Outcome.UNKNOWN_CHAT, answer.detail)
            sleep(UNKNOWN_RETRY_INTERVAL_SECONDS)
            continue
        if answer.kind == INPUT_BLOCKED_KIND:
            return SendResult(Outcome.BLOCKED, answer.detail)
        return SendResult(Outcome.REFUSED, answer.detail)


def send_through_mngr(chat_id: str, text: str) -> int:
    """The backoff: ``mngr message`` straight to the agent, its exit status passed through."""
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", delete=False
    ) as message_file:
        message_file.write(text)
        message_path = message_file.name
    try:
        completed = subprocess.run(
            ["mngr", "message", chat_id, "--message-file", message_path],
            check=False,
        )
    finally:
        os.unlink(message_path)
    return completed.returncode


def _read_message(args: argparse.Namespace, stdin: IO[str]) -> str:
    if args.message is not None:
        return args.message
    if args.message_file is not None:
        return Path(args.message_file).read_text(encoding="utf-8")
    if stdin.isatty():
        raise SystemExit(
            "error: no message given (use -m, --message-file, or pipe the text on stdin)"
        )
    return stdin.read()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a message to a chat through the chat app, falling back to `mngr message` only when the chat app cannot take it.",
    )
    parser.add_argument(
        "chat_id",
        help="The chat's id (its agent id, e.g. $MNGR_AGENT_ID); never a name.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("-m", "--message", help="The message text.")
    source.add_argument("--message-file", help="A file whose contents are the message.")
    parser.add_argument(
        "--system",
        action="store_true",
        help="Mark the message as an automated nudge, rendered as a collapsed chip in the chat.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _build_parser().parse_args(argv)
    resolved_environ = os.environ if environ is None else environ
    text = _read_message(args, sys.stdin if stdin is None else stdin)
    if args.system:
        text = wrap_system_message(text)
    base_url = chat_app_url(resolved_environ, Path.cwd())
    result = send_through_chat_app(
        base_url, args.chat_id, text, uuid.uuid4().hex, clock, sleep
    )
    if result.outcome is Outcome.DELIVERED:
        print(f"Sent to chat {args.chat_id} through the chat app")
        return EXIT_DELIVERED
    if result.outcome is Outcome.BLOCKED:
        print(
            f"Delivered to chat {args.chat_id}, but its input is blocked: {result.detail}",
            file=sys.stderr,
        )
        return EXIT_DELIVERED_BUT_BLOCKED
    if result.outcome is Outcome.REFUSED:
        print(
            f"The chat app refused the message for chat {args.chat_id}: {result.detail}",
            file=sys.stderr,
        )
        return EXIT_FAILED
    # UNREACHABLE or UNKNOWN_CHAT: the chat app cannot take this message, so mngr delivers it.
    print(
        f"Falling back to `mngr message` for chat {args.chat_id}: {result.detail}",
        file=sys.stderr,
    )
    return send_through_mngr(args.chat_id, text)


if __name__ == "__main__":
    raise SystemExit(main())
