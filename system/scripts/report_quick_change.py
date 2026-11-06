#!/usr/bin/env python3
"""Report the outcome of a quick look-and-feel change, from the quick-change agent.

Usage, from the repo root::

    python3 system/scripts/report_quick_change.py <request-id> done "Todo: header is now dark blue; window refreshed."
    python3 system/scripts/report_quick_change.py <request-id> failed "Todo: could not find the header styles."

The report is posted to the chat app's quick-change report route, which appends it to the
quick-change log (``data/.apps/chat/quick_changes.jsonl``) next to the request it answers and shows
it in the chat the request came from. When the chat app cannot be reached, the report is appended
to the log directly so it is never lost.

Standard library only, like ``message_chat.py``; the chat app is found the same way (its row in the
app registry, else its fixed port).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_APPS_FILE = "data/.state/apps.toml"
ENV_APPS_FILE = "MINDS_APPS_FILE"
CHAT_APP_NAME = "chat"
CHAT_APP_FALLBACK_URL = "http://127.0.0.1:8010"
# Mirrors ``QUICK_CHANGES_LOG_FILENAME`` in the chat app's ``chat_quick_change.py`` under its data folder.
DEFAULT_LOG_PATH = "data/.apps/chat/quick_changes.jsonl"
REPORT_PATH = "/api/quick-changes/{}/report"
STATUSES = ("done", "failed")
TIMEOUT_SECONDS = 10.0


def chat_app_url(apps_file: Path) -> str:
    """The chat app's base URL from the app registry, else its fixed port."""
    try:
        registry = tomllib.loads(apps_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return CHAT_APP_FALLBACK_URL
    for row in registry.get("apps", []):
        if (
            isinstance(row, dict)
            and row.get("name") == CHAT_APP_NAME
            and isinstance(row.get("url"), str)
        ):
            return row["url"].rstrip("/")
    return CHAT_APP_FALLBACK_URL


def report_record(request_id: str, status: str, report: str) -> dict[str, str]:
    return {
        "type": "report",
        "request_id": request_id,
        "status": status,
        "report": report,
        "reported_at": datetime.now(timezone.utc).isoformat(),
    }


def post_report(base_url: str, request_id: str, status: str, report: str) -> bool:
    """True once the chat app took the report."""
    body = json.dumps({"status": status, "report": report}).encode("utf-8")
    request = urllib.request.Request(
        base_url + REPORT_PATH.format(request_id),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as e:
        print(
            f"The chat app did not take the report ({e}); writing it to the log directly.",
            file=sys.stderr,
        )
        return False


def append_record(log_path: Path, record: dict[str, str]) -> None:
    """One write of one line, so a line the chat app appends at the same time never interleaves with it."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("request_id", help="the request id from the forwarded message")
    parser.add_argument("status", choices=STATUSES)
    parser.add_argument(
        "report", help="one or two plain sentences: what changed, in which app"
    )
    parser.add_argument(
        "--log",
        default=DEFAULT_LOG_PATH,
        help="the quick-change log, for the direct fallback",
    )
    args = parser.parse_args(argv)
    report = " ".join(args.report.split())
    if report == "":
        parser.error("the report is empty")
    base_url = chat_app_url(Path(os.environ.get(ENV_APPS_FILE, DEFAULT_APPS_FILE)))
    if not post_report(base_url, args.request_id, args.status, report):
        append_record(
            Path(args.log), report_record(args.request_id, args.status, report)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
