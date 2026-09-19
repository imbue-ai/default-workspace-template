#!/usr/bin/env python3
"""Ask the user for a secret through the chat's secret card.

Usage, from the repo root, ALONE in its tool call with its output untouched::

    python3 .agents/skills/connect-external-service/scripts/request_secret.py \\
        --file <name> --var NAME [--var NAME ...] --rationale "..."

Files the request with the chat app, which renders a card with one password input
per variable in this chat. On Submit the chat app writes
``data/.secrets/<name>.env`` (merging the named variables into any existing file)
and sends this chat a message naming the file and the variables, never the values;
on Decline it sends the verdict and the user's note. Nothing about the value ever
passes through this script or the transcript.

Prints the filed request as JSON (``request_id``, ``file``, ``variables``,
``existing_variables``, ``overwrites``, ...) -- the chat builds the card from that
echo, which is why the call must stand alone (policy P9). End the turn after it
prints; the resolution arrives as a message.

Exits 1 with a plain message when the chat app cannot be reached or refuses the
request, or when neither ``MINDS_CHAT_ID`` nor ``MNGR_AGENT_ID`` is set. With no chat
app there is no card; the skill's last resort is to ask the user to place the file
from a terminal.

Standard library only, like ``system/scripts/message_chat.py``, which this reuses to
find the chat app.
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

# `.claude/skills` is a symlink to `.agents/skills`; resolving through it lands on the
# real file, four directories below the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MESSAGE_CHAT_SCRIPT = _REPO_ROOT / "system" / "scripts" / "message_chat.py"

FILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

REQUESTS_PATH = "/api/secret-requests"

EXIT_FILED = 0
EXIT_FAILED = 1

NOT_READY_RETRY_WINDOW_SECONDS = 30.0
NOT_READY_RETRY_INTERVAL_SECONDS = 1.0


def _load_message_chat() -> Any:
    """The chat-app locator and poster from message_chat.py, imported by path (the scripts are not a package)."""
    spec = importlib.util.spec_from_file_location(
        "message_chat_for_request_secret", _MESSAGE_CHAT_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise SystemExit(f"request_secret: cannot load {_MESSAGE_CHAT_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask the user for a secret through the chat's secret card; prints the filed request as JSON.",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="The <name> of data/.secrets/<name>.env: lowercase letters, digits, hyphens.",
    )
    parser.add_argument(
        "--var",
        action="append",
        dest="variables",
        default=[],
        metavar="NAME",
        help="A variable the card asks for (repeatable). A POSIX identifier, conventionally UPPER_CASE.",
    )
    parser.add_argument(
        "--rationale",
        required=True,
        help="Why you need it, in the user's terms; shown on the card.",
    )
    return parser


def validate_arguments(file: str, variables: list[str], rationale: str) -> str | None:
    """The reason the arguments are unusable, or None."""
    if not FILE_NAME_RE.match(file):
        return f"--file {file!r} must be lowercase letters, digits and hyphens, starting with a letter or digit"
    if not variables:
        return "at least one --var NAME is required"
    for name in variables:
        if not VARIABLE_NAME_RE.match(name):
            return f"--var {name!r} must be letters, digits and underscores, not starting with a digit"
    if len(set(variables)) != len(variables):
        return "--var names must be distinct"
    if not rationale.strip():
        return "--rationale must not be empty"
    return None


def chat_id_from_environment(environ: Mapping[str, str]) -> str | None:
    return environ.get("MINDS_CHAT_ID") or environ.get("MNGR_AGENT_ID") or None


def file_request(
    message_chat: Any,
    base_url: str,
    body: Mapping[str, object],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[int, object]:
    """POST the request, retrying a not-ready chat app for a bounded window; returns (status, body)."""
    started_at = clock()
    while True:
        answer = message_chat.post_json(base_url, REQUESTS_PATH, body)
        if (
            answer.status != 503
            or clock() - started_at >= NOT_READY_RETRY_WINDOW_SECONDS
        ):
            return answer.status, answer.body
        sleep(NOT_READY_RETRY_INTERVAL_SECONDS)


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    problem = validate_arguments(args.file, args.variables, args.rationale)
    if problem is not None:
        parser.error(problem)
    resolved_environ = os.environ if environ is None else environ
    chat_id = chat_id_from_environment(resolved_environ)
    if chat_id is None:
        print(
            "request_secret: neither MINDS_CHAT_ID nor MNGR_AGENT_ID is set; run this from an agent shell.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    message_chat = _load_message_chat()
    base_url = message_chat.chat_app_url(resolved_environ, Path.cwd())
    body = {
        "chat_id": chat_id,
        "file": args.file,
        "variables": args.variables,
        "rationale": args.rationale,
    }
    try:
        status, answer = file_request(message_chat, base_url, body, clock, sleep)
    except message_chat.ChatAppUnreachableError as exc:
        print(
            f"request_secret: {exc}. There is no chat app to show a secret card, so the secret cannot be "
            "requested this way; ask the user to place data/.secrets/<name>.env from a terminal instead.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    except (OSError, http.client.HTTPException) as exc:
        print(
            f"request_secret: the chat app dropped the request: {exc}", file=sys.stderr
        )
        return EXIT_FAILED
    if not 200 <= status < 300:
        detail = answer.get("detail") if isinstance(answer, dict) else None
        print(
            f"request_secret: the chat app refused the request (HTTP {status}): {detail or answer}",
            file=sys.stderr,
        )
        return EXIT_FAILED
    print(json.dumps(answer, indent=2))
    return EXIT_FILED


if __name__ == "__main__":
    raise SystemExit(main())
