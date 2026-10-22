#!/usr/bin/env python3
"""Ask the running Datalib app to sync, and wait for a sync it was asked for.

Usage, from the repo root::

    python3 .agents/skills/datalib/scripts/sync_request.py open slack/ingest
    python3 system/scripts/run_in_background.py --description "Sync Slack into datalib" -- \\
        python3 .agents/skills/datalib/scripts/sync_request.py wait <request-id>...

``open`` files the sync with ``datalib-http`` and prints one request id per line. The sync then
runs inside that server, which supervisord runs independently of any chat, so stopping or handing
off the chat that asked does not stop it. ``wait`` polls until every named request has ended and
prints how; under ``run_in_background.py`` that output is what wakes the agent.

``wait`` exits the way ``datalib-dag`` does for the same outcomes: 0 when every request is done,
2 when one failed, 130 when one was stopped; 1 when the server could not be asked.

Standard library only: it runs under a plain ``python3``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

DEFAULT_ORIGIN = "http://127.0.0.1:8731"
DEFAULT_DATA_ROOT = Path("data/.skills/datalib")
TOKEN_FILE_RELATIVE_PATH = Path("system") / "api-token"

POLL_INTERVAL_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 30.0
# datalib-http restarts (an update, a shed for memory) while a sync is open, and supervisord
# brings it back; a wait gives up only once it has been unreachable for this long in one stretch.
UNREACHABLE_BUDGET_SECONDS = 600.0

STATE_OPEN = "open"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_STOPPED = "stopped"
# A request the server no longer lists: it shows the open ones and only the newest closed ones.
STATE_NOT_LISTED = "not listed"

EXIT_DONE = 0
EXIT_ERROR = 1
EXIT_FAILED = 2
EXIT_STOPPED = 130

Fetch = Callable[[str, bytes | None], Any]


class SyncRequestError(Exception):
    """The Datalib app could not be asked; the message says why."""


def read_token(data_root: Path) -> str:
    path = data_root / TOKEN_FILE_RELATIVE_PATH
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SyncRequestError(
            f"cannot read the API token at {path} ({e}); is the Datalib app running? `bash .agents/skills/datalib/scripts/install_datalib.sh` installs and starts it"
        ) from e
    if not token:
        raise SyncRequestError(f"the API token file {path} is empty")
    return token


def http_fetch(origin: str, token: str) -> Fetch:
    """A fetch of ``origin``'s JSON API with the bearer token: GET with no body, POST with one."""
    # No proxy: the server is on loopback, and the workspace's proxy variables do not apply to it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(path: str, body: bytes | None) -> Any:
        request = urllib.request.Request(f"{origin}{path}", data=body)
        request.add_header("Authorization", f"Bearer {token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace").strip()
            raise SyncRequestError(f"{path} answered {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SyncRequestError(f"{origin}{path} could not be read: {e}") from e

    return fetch


def open_requests(fetch: Fetch, roots: Sequence[str], by: str) -> list[dict[str, Any]]:
    """File a sync of ``roots`` (none: every source) and return the requests the server opened for it."""
    body = json.dumps({"roots": list(roots), "by": by}).encode()
    return list(fetch("/api/requests", body))


def states_of(
    listed: Sequence[dict[str, Any]], request_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Each asked-for request as the server lists it now, or as ``not listed``."""
    by_id = {request["id"]: request for request in listed}
    return {
        request_id: by_id.get(request_id, {"id": request_id, "state": STATE_NOT_LISTED})
        for request_id in request_ids
    }


def is_settled(states: dict[str, dict[str, Any]]) -> bool:
    return all(request["state"] != STATE_OPEN for request in states.values())


def exit_code_of(states: dict[str, dict[str, Any]]) -> int:
    outcomes = {request["state"] for request in states.values()}
    if STATE_FAILED in outcomes:
        return EXIT_FAILED
    if STATE_STOPPED in outcomes:
        return EXIT_STOPPED
    if outcomes <= {STATE_DONE}:
        return EXIT_DONE
    # `closed` (an outcome a newer datalib wrote) or `not listed`: ended, but not known to have worked.
    return EXIT_ERROR


def describe(request: dict[str, Any]) -> str:
    line = f"{request['id']}  {request['state']}"
    if request.get("roots"):
        line += f"  {' '.join(request['roots'])}"
    if request.get("failed_step"):
        line += f"  (failed at {request['failed_step']})"
    if request.get("stop_requested_by"):
        line += f"  (stopped by {request['stop_requested_by']})"
    if request["state"] == STATE_NOT_LISTED:
        line += "  (ended and aged out of GET /api/requests; the Manage screen or /api/runs has the outcome)"
    return line


def wait_for_requests(
    fetch: Fetch,
    request_ids: Sequence[str],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, Any]]:
    """Poll until none of ``request_ids`` is open, riding out a server that is briefly unreachable."""
    unreachable_since: float | None = None
    while True:
        try:
            states = states_of(fetch("/api/requests", None), request_ids)
        except SyncRequestError as e:
            now = clock()
            unreachable_since = now if unreachable_since is None else unreachable_since
            if now - unreachable_since >= UNREACHABLE_BUDGET_SECONDS:
                raise SyncRequestError(
                    f"the Datalib app has not answered for {int(now - unreachable_since)}s; last error: {e}"
                ) from e
        else:
            unreachable_since = None
            if is_settled(states):
                return states
        sleep(POLL_INTERVAL_SECONDS)


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask the running Datalib app to sync, or wait for a sync"
    )
    parser.add_argument(
        "--origin", default=DEFAULT_ORIGIN, help="Where datalib-http listens"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help="The datalib data root",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    open_parser = commands.add_parser(
        "open", help="File a sync; prints one request id per line"
    )
    open_parser.add_argument(
        "roots",
        nargs="*",
        help="Step ids such as slack/ingest; none syncs every source",
    )
    open_parser.add_argument(
        "--by", default="agent", help="Who is asking, shown on the Manage screen"
    )
    wait_parser = commands.add_parser(
        "wait", help="Wait until the named requests have ended"
    )
    wait_parser.add_argument("request_ids", nargs="+", help="Ids printed by `open`")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    arguments = parse_arguments(argv)
    try:
        fetch = http_fetch(arguments.origin, read_token(arguments.data_root))
        if arguments.command == "open":
            for request in open_requests(fetch, arguments.roots, arguments.by):
                sys.stdout.write(f"{request['id']}\n")
            return EXIT_DONE
        states = wait_for_requests(fetch, arguments.request_ids)
    except SyncRequestError as e:
        sys.stderr.write(f"sync_request: {e}\n")
        return EXIT_ERROR
    for request in states.values():
        sys.stdout.write(f"{describe(request)}\n")
    return exit_code_of(states)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
