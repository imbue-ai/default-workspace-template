#!/usr/bin/env python3
"""The record of every app published to GitHub Pages from this workspace.

    uv run --no-sync ledger.py get --app <name>
    uv run --no-sync ledger.py list
    uv run --no-sync ledger.py record --app <name> --owner <username> --repo <repo> --url <site url> \\
        --published-commit <sha> [--data "<what data the site carries>"]
    uv run --no-sync ledger.py remove --app <name>

Kept at `data/.skills/publish-to-github-pages/sites.json`, one entry per app.
`get` prints the app's entry as JSON and exits 1 when it has none (a first
publish); `record` adds or replaces it, stamping the time and the workspace
commit the copy was built from.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

LEDGER_PATH = Path("data/.skills/publish-to-github-pages/sites.json")


def load(path: Path = LEDGER_PATH) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(entries: dict[str, dict[str, object]], path: Path = LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _workspace_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="The record of published GitHub Pages sites."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("get").add_argument("--app", required=True)
    commands.add_parser("list")
    record = commands.add_parser("record")
    for name in ("--app", "--owner", "--repo", "--url", "--published-commit"):
        record.add_argument(name, required=True)
    record.add_argument(
        "--data", default="none", help="what data the site carries, in a few words"
    )
    commands.add_parser("remove").add_argument("--app", required=True)
    args = parser.parse_args(argv)

    entries = load()
    if args.command == "get":
        if args.app not in entries:
            print(f"ledger: {args.app!r} has not been published", file=sys.stderr)
            return 1
        print(json.dumps(entries[args.app], indent=2))
    elif args.command == "list":
        print(json.dumps(entries, indent=2, sort_keys=True))
    elif args.command == "record":
        entries[args.app] = {
            "app": args.app,
            "owner": args.owner,
            "repo": args.repo,
            "url": args.url,
            "published_commit": args.published_commit,
            "workspace_commit": _workspace_commit(),
            "data": args.data,
            "published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            ),
        }
        save(entries)
        print(f"ledger: recorded {args.app} at {args.url}")
    elif args.command == "remove":
        entries.pop(args.app, None)
        save(entries)
        print(f"ledger: removed {args.app}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
