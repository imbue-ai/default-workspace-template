#!/usr/bin/env python3
"""Send a message to a chat, by chat id, through the chat app; or create a chat there.

Usage, from the repo root (every skill's cwd)::

    python3 system/scripts/message_chat.py <chat-id> -m "text"
    python3 system/scripts/message_chat.py <chat-id> --message-file path/to/task.md
    some-command | python3 system/scripts/message_chat.py <chat-id>
    python3 system/scripts/message_chat.py <chat-id> --system -m "an automated nudge"
    python3 system/scripts/message_chat.py --create --name "assist-1a2b3c" --label auto_open=true -m "/assist ..."

This is the in-workspace replacement for ``mngr message <agent>``. A chat is
addressed by its chat id (``$MINDS_CHAT_ID`` on an agent the chat app created; the
id of its first agent, so ``$MNGR_AGENT_ID`` for an agent that is its own chat),
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
already put the text in the pane. The backoff passes ``--start``: the chat app's
route revives a stopped agent on send, so the backoff does the same.

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

``--create`` makes a new chat instead of messaging one, through the chat app's
create route, so the chat is what a New Tab chat would be: the app mints its id,
binds it to the workspace's default account and harness, names it (``--name``,
else the next free "Chat N"), and sends the message as its first one. ``--label``
adds a label to the chat's agent (``auto_open=true`` has the workspace open its
tab); ``--skip-installation-check`` lets the create through a claude version
check the workspace would otherwise fail, for the update run that repairs it.
The script waits for the chat app to finish the create and exits 0 with one
JSON line on stdout, ``{"chat_id", "name", "display_name"}``, or 1 with the
create's own failure on stderr. The backoff is a plain ``mngr create --template
chat`` with the same name, labels, and message, taken on the same terms as the
send's: only when the chat app cannot be reached, has no create route, or has a
create route from before these fields (which refuses them by name).

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
from typing import IO, assert_never

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

# The route's ``kind`` for a send that landed behind a dialog
# (``SendFailureKind.INPUT_BLOCKED`` in mngr).
INPUT_BLOCKED_KIND = "input_blocked"

CREATE_CHAT_PATH = "/api/chats/create"
# The create route's field that asks it to answer once the create has finished; a route
# from before it rejects the field by name.
WAIT_FIELD = "should_wait"

# Mirrors ``SKIP_CLAUDE_INSTALLATION_CHECK_SETTING`` in the chat app's ``agent_manager.py``,
# the setting the create route applies for ``is_installation_check_skipped``.
SKIP_CLAUDE_INSTALLATION_CHECK_SETTING = "agent_types.claude.check_installation=false"

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
    """How a request to the chat app ended."""

    DELIVERED = "delivered"
    BLOCKED = "blocked"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"
    # Nothing here can take the request, so the backoff does: a 404 that outlasted its
    # window (a chat the app does not know, or a chat app from before the route), or a
    # create route from before the fields a create sends, which refuses them by name.
    NOT_FOUND = "not_found"


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


class CreatedChat:
    """The chat the create route made: its id and name pair."""

    def __init__(self, chat_id: str, name: str, display_name: str) -> None:
        self.chat_id = chat_id
        self.name = name
        self.display_name = display_name

    def as_json_line(self) -> str:
        return json.dumps(
            {
                "chat_id": self.chat_id,
                "name": self.name,
                "display_name": self.display_name,
            }
        )


class ChatAppUnreachableError(Exception):
    """The connection to the chat app could not be made at all.

    Its own class, not ``ConnectionError``: the stdlib raises ``ConnectionError`` subclasses
    for drops *after* the connect too (``http.client.RemoteDisconnected`` is a
    ``ConnectionResetError``), and those must never be read as "unreachable", because the
    request may already have been acted on.
    """


def wrap_system_message(text: str) -> str:
    """Wrap an automated nudge in the sentinel; adds no newlines, so the wrapped text types into a pane like the bare text."""
    return f"<{SYSTEM_MESSAGE_TAG}>{text}</{SYSTEM_MESSAGE_TAG}>"


def chat_app_url(environ: Mapping[str, str], cwd: Path) -> str:
    """The chat app's base URL: its registry row, else the fixed fallback.

    A missing, unreadable, or rowless registry reads as "not registered yet", never as an
    error: the fallback is the port the chat app has always used. A registry that exists
    but does not parse takes the same fallback, noted on stderr.
    """
    apps_file = Path(environ.get(ENV_APPS_FILE) or DEFAULT_APPS_FILE)
    if not apps_file.is_absolute():
        apps_file = cwd / apps_file
    try:
        rows = tomllib.loads(apps_file.read_text(encoding="utf-8")).get("apps", [])
    except OSError:
        return CHAT_APP_FALLBACK_URL
    except tomllib.TOMLDecodeError as exc:
        print(
            f"The app registry at {apps_file} does not parse ({exc}); "
            f"using the chat app's fixed address {CHAT_APP_FALLBACK_URL}",
            file=sys.stderr,
        )
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


def post_json(base_url: str, path: str, body: Mapping[str, object]) -> ChatAppAnswer:
    """POST ``body`` and wait for the answer, however long it takes.

    Raises ``ChatAppUnreachableError`` only when the connection itself cannot be made;
    anything after the connect is either an answer or an ``OSError`` /
    ``http.client.HTTPException``, which the caller treats as a failure rather than a
    reason to back off, because the request may have been acted on.
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
        raise ChatAppUnreachableError(
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


def _post_until_answered(
    base_url: str,
    path: str,
    body: Mapping[str, object],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> ChatAppAnswer | SendResult:
    """POST until the chat app gives an answer that is not a not-ready or not-found one.

    The 503 and 404 answers are retried within their windows; a connection that cannot be
    made, a request dropped after the connect, and a window that ran out come back as the
    ``SendResult`` they mean. Anything else is the chat app's answer, the caller's to read.
    """
    started_at = clock()
    unknown_since: float | None = None
    while True:
        try:
            answer = post_json(base_url, path, body)
        except ChatAppUnreachableError as exc:
            return SendResult(Outcome.UNREACHABLE, str(exc))
        except (OSError, http.client.HTTPException) as exc:
            return SendResult(
                Outcome.REFUSED, f"the chat app dropped the request: {exc}"
            )
        if answer.status == 503:
            if clock() - started_at >= NOT_READY_RETRY_WINDOW_SECONDS:
                return SendResult(Outcome.REFUSED, answer.detail)
            sleep(NOT_READY_RETRY_INTERVAL_SECONDS)
            continue
        if answer.status == 404:
            if unknown_since is None:
                unknown_since = clock()
            if clock() - unknown_since >= UNKNOWN_RETRY_WINDOW_SECONDS:
                return SendResult(Outcome.NOT_FOUND, answer.detail)
            sleep(UNKNOWN_RETRY_INTERVAL_SECONDS)
            continue
        return answer


def send_through_chat_app(
    base_url: str,
    chat_id: str,
    text: str,
    message_id: str,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> SendResult:
    """Post the message to the chat app's send route, retrying the not-ready answers, and report how it ended."""
    path = f"/api/chats/{urllib.parse.quote(chat_id, safe='')}/message"
    body = {"message": text, "message_id": message_id}
    answer = _post_until_answered(base_url, path, body, clock, sleep)
    if isinstance(answer, SendResult):
        return answer
    if 200 <= answer.status < 300:
        return SendResult(Outcome.DELIVERED, "")
    if answer.kind == INPUT_BLOCKED_KIND:
        return SendResult(Outcome.BLOCKED, answer.detail)
    return SendResult(Outcome.REFUSED, answer.detail)


def create_request_body(request: CreateRequest) -> dict[str, object]:
    """The JSON the create route is posted: the chat app's ``CreateChatRequest``, which forbids
    unknown fields, so a test there pins these keys against it."""
    return {
        "name": request.name,
        "message": request.message,
        "labels": dict(request.labels),
        "is_installation_check_skipped": request.is_installation_check_skipped,
        WAIT_FIELD: True,
    }


def create_through_chat_app(
    base_url: str,
    request: CreateRequest,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> CreatedChat | SendResult:
    """Post the create to the chat app and wait for its verdict: the chat, or how it was not made.

    The route is asked to answer only once the create has finished, so the read blocks for
    as long as ``mngr create`` takes (the connect timeout alone bounds an unreachable app),
    and a 2xx means the chat runs.
    """
    answer = _post_until_answered(
        base_url, CREATE_CHAT_PATH, create_request_body(request), clock, sleep
    )
    if isinstance(answer, SendResult):
        return answer
    if answer.status == 400 and WAIT_FIELD in answer.detail:
        # The route predates these fields (a chat app not yet restarted after an update):
        # its refusal names the field it does not know, and mngr makes the chat instead.
        return SendResult(Outcome.NOT_FOUND, answer.detail)
    if not 200 <= answer.status < 300:
        return SendResult(Outcome.REFUSED, answer.detail)
    if not isinstance(answer.body, dict) or not isinstance(
        answer.body.get("chat_id"), str
    ):
        return SendResult(
            Outcome.REFUSED, "the chat app created the chat but named no chat id"
        )
    return CreatedChat(
        chat_id=answer.body["chat_id"],
        name=str(answer.body.get("name") or ""),
        display_name=str(answer.body.get("display_name") or ""),
    )


class CreateRequest:
    """What ``--create`` asks for: the chat's name, labels, message, and the version-check waiver."""

    def __init__(
        self,
        name: str,
        message: str,
        labels: Mapping[str, str],
        is_installation_check_skipped: bool,
    ) -> None:
        self.name = name
        self.message = message
        self.labels = dict(labels)
        self.is_installation_check_skipped = is_installation_check_skipped


def _run_mngr(
    argv_before_file: list[str],
    text: str | None,
    argv_after_file: list[str],
    capture_stdout: bool,
) -> subprocess.CompletedProcess[str] | None:
    """Run ``mngr``, with ``text`` in a file named by ``--message-file``; None when there is no ``mngr`` to run.

    ``text`` of None runs mngr with no message at all, for a create that seeds none.
    """
    message_path: str | None = None
    if text is not None:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".md", delete=False
        ) as message_file:
            message_file.write(text)
            message_path = message_file.name
    message_argv = [] if message_path is None else ["--message-file", message_path]
    try:
        return subprocess.run(
            [*argv_before_file, *message_argv, *argv_after_file],
            check=False,
            stdout=subprocess.PIPE if capture_stdout else None,
            text=True,
        )
    except FileNotFoundError as exc:
        print(f"The backoff could not run `mngr`: {exc}", file=sys.stderr)
        return None
    finally:
        if message_path is not None:
            os.unlink(message_path)


def _script_exit_for_mngr_exit(returncode: int) -> int:
    """A backoff ``mngr`` run's status in THIS script's vocabulary: 0 landed, 7 landed behind a
    dialog, 1 for every other way it did not.

    mngr has statuses this script's does not, and one of them collides: mngr exits 2 on a
    timeout, while 2 from this script means it never ran or never understood the request
    (python's on a missing file, argparse's on an argument an older copy lacks). A caller
    reads that 2 as "this template has no such script" and runs its own ``mngr`` instead --
    so passing mngr's own 2 through would answer a timed-out create with a second create.
    mngr's account of the failure still reaches the caller on stderr either way.
    """
    if returncode in (EXIT_DELIVERED, EXIT_DELIVERED_BUT_BLOCKED):
        return returncode
    return EXIT_FAILED


def send_through_mngr(chat_id: str, text: str) -> int:
    """The backoff: ``mngr message --start`` straight to the agent, its verdict in this script's codes."""
    completed = _run_mngr(
        ["mngr", "message", chat_id, "--start"], text, [], capture_stdout=False
    )
    return (
        EXIT_FAILED
        if completed is None
        else _script_exit_for_mngr_exit(completed.returncode)
    )


def _created_agent_id(create_stdout: str) -> str:
    """The agent id from ``mngr create --format jsonl``'s ``created`` event; '' when it named none."""
    for line in create_stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "created":
            agent_id = event.get("agent_id")
            if isinstance(agent_id, str):
                return agent_id
    return ""


def create_through_mngr(request: CreateRequest) -> int:
    """The backoff: a plain ``mngr create --template chat`` in the workspace, printing the same JSON line.

    The workspace's own create defaults bind the chat to the default account and harness,
    as they do for every create here that names neither. ``user_created`` puts the chat in
    the OOM band the chat app's own creates get.
    """
    argv = ["mngr", "create", *([request.name] if request.name else [])]
    argv += ["--template", "chat", "--no-connect", "--label", "user_created=true"]
    for key, value in request.labels.items():
        argv += ["--label", f"{key}={value}"]
    if request.is_installation_check_skipped:
        argv += ["-S", SKIP_CLAUDE_INSTALLATION_CHECK_SETTING]
    completed = _run_mngr(
        argv, request.message or None, ["--format", "jsonl"], capture_stdout=True
    )
    if completed is None:
        return EXIT_FAILED
    if completed.returncode != 0:
        return _script_exit_for_mngr_exit(completed.returncode)
    chat_id = _created_agent_id(completed.stdout)
    if not chat_id:
        # The chat is made -- mngr exited 0 -- so this is not a failure, but the JSON line
        # below is the caller's only handle on it and it is about to carry an empty id.
        print(
            "`mngr create` named no chat in its output; the chat exists but its id is unknown",
            file=sys.stderr,
        )
    created = CreatedChat(chat_id=chat_id, name=request.name, display_name=request.name)
    print(created.as_json_line())
    return EXIT_DELIVERED


def _read_message(
    parser: argparse.ArgumentParser, args: argparse.Namespace, stdin: IO[str]
) -> str:
    if args.message is not None:
        return args.message
    if args.message_file is not None:
        return Path(args.message_file).read_text(encoding="utf-8")
    if stdin.isatty():
        if args.create:
            # A create needs no first message; the chat then just opens.
            return ""
        parser.error(
            "no message given (use -m, --message-file, or pipe the text on stdin)"
        )
    return stdin.read()


def _parse_labels(
    parser: argparse.ArgumentParser, raw_labels: list[str]
) -> dict[str, str]:
    labels: dict[str, str] = {}
    for raw in raw_labels:
        key, separator, value = raw.partition("=")
        if not separator or not key:
            parser.error(f"--label takes NAME=VALUE, not {raw!r}")
        labels[key] = value
    return labels


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Send a message to a chat through the chat app, falling back to `mngr message` only when the "
            "chat app cannot take it; or, with --create, make a new chat there."
        ),
    )
    parser.add_argument(
        "chat_id",
        nargs="?",
        help="The chat's id ($MINDS_CHAT_ID, or the id of an agent that is its own chat); never a name.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("-m", "--message", help="The message text.")
    source.add_argument("--message-file", help="A file whose contents are the message.")
    parser.add_argument(
        "--system",
        action="store_true",
        help="Mark the message as an automated nudge, rendered as a collapsed chip in the chat.",
    )
    create = parser.add_argument_group("creating a chat")
    create.add_argument(
        "--create",
        action="store_true",
        help="Create a new chat (no chat id) whose first message is the one given, and print its id as JSON.",
    )
    create.add_argument(
        "--name",
        default="",
        help="The new chat's name; the chat app mints one when absent.",
    )
    create.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A label for the new chat's agent (auto_open=true opens its tab); repeatable.",
    )
    create.add_argument(
        "--skip-installation-check",
        action="store_true",
        help="Create the chat past a claude version check the workspace would otherwise fail.",
    )
    return parser


def _validate_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.create:
        if args.chat_id is not None:
            parser.error("--create makes a new chat; it takes no chat id")
        if args.system:
            parser.error(
                "--system marks a nudge to an existing chat; it does not apply to --create"
            )
        return
    if args.chat_id is None:
        parser.error("a chat id is required (or --create to make a new chat)")
    if args.name or args.label or args.skip_installation_check:
        parser.error(
            "--name, --label, and --skip-installation-check apply only with --create"
        )


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    stdin: IO[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_mode(parser, args)
    resolved_environ = os.environ if environ is None else environ
    text = _read_message(parser, args, sys.stdin if stdin is None else stdin)
    if args.system:
        text = wrap_system_message(text)
    base_url = chat_app_url(resolved_environ, Path.cwd())
    if args.create:
        request = CreateRequest(
            name=args.name,
            message=text,
            labels=_parse_labels(parser, args.label),
            is_installation_check_skipped=args.skip_installation_check,
        )
        return _create(base_url, request, clock, sleep)
    result = send_through_chat_app(
        base_url, args.chat_id, text, uuid.uuid4().hex, clock, sleep
    )
    match result.outcome:
        case Outcome.DELIVERED:
            print(f"Sent to chat {args.chat_id} through the chat app")
            return EXIT_DELIVERED
        case Outcome.BLOCKED:
            print(
                f"Delivered to chat {args.chat_id}, but its input is blocked: {result.detail}",
                file=sys.stderr,
            )
            return EXIT_DELIVERED_BUT_BLOCKED
        case Outcome.REFUSED:
            print(
                f"The chat app refused the message for chat {args.chat_id}: {result.detail}",
                file=sys.stderr,
            )
            return EXIT_FAILED
        case Outcome.UNREACHABLE | Outcome.NOT_FOUND:
            # The chat app cannot take this message, so mngr delivers it.
            print(
                f"Falling back to `mngr message` for chat {args.chat_id}: {result.detail}",
                file=sys.stderr,
            )
            return send_through_mngr(args.chat_id, text)
        case _ as unreachable:
            assert_never(unreachable)


def _create(
    base_url: str,
    request: CreateRequest,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> int:
    result = create_through_chat_app(base_url, request, clock, sleep)
    if isinstance(result, CreatedChat):
        print(result.as_json_line())
        return EXIT_DELIVERED
    match result.outcome:
        case Outcome.REFUSED | Outcome.BLOCKED | Outcome.DELIVERED:
            print(
                f"The chat app did not create the chat: {result.detail}",
                file=sys.stderr,
            )
            return EXIT_FAILED
        case Outcome.UNREACHABLE | Outcome.NOT_FOUND:
            # No chat app to make the chat, or one from before the route: mngr makes it.
            print(f"Falling back to `mngr create`: {result.detail}", file=sys.stderr)
            return create_through_mngr(request)
        case _ as unreachable:
            assert_never(unreachable)


if __name__ == "__main__":
    raise SystemExit(main())
