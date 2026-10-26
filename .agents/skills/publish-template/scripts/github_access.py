#!/usr/bin/env python3
"""Probe, and wait for, the GitHub access the publish flows need through latchkey.

Two kinds of access, each its own latchkey scope:

  - `api`: the GitHub REST API (`github-rest-api` with `github-read-user` +
    `github-write-all`) -- repo creation, settings, topics. Probed by reading
    `/user`; the gateway answers an unpermitted request with a 403, which
    `curl -f` turns into a non-zero exit.
  - `git-write`: a push through the gateway's git proxy (`github-git` with
    `github-git-write`). Probed from the gateway's own permission rules, where an
    approved grant is keyed `github-git:<account>` (never bare `github-git`) or
    by the catch-all `any`.

This only ever reads. The permission REQUESTS stay inline in the skill: the chat
builds its approval card from a request's own command line and echoed result, so a
request run from inside a script would leave the user with no button to press.

Usage:

    python3 github_access.py check [--need api] [--need git-write]
    python3 github_access.py wait [--need ...] [--timeout-seconds 300] [--interval-seconds 10]

`--need` defaults to both. Each prints one line per kind and exits 0 only when
all are permitted. A kind is UNKNOWN, not "NOT permitted", when the gateway
brings back no readable answer -- an error instead of its rules (typically that
it cannot reach the user's Imbue Studio app, which a permission request would
need too), a failed latchkey run, or output that is not JSON. `check` exits
2 when anything is unknown, so a caller files no request until it can see the
whole picture; `wait` keeps polling through it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum

_API_PROBE_URL = "https://api.github.com/user"
_PERMISSIONS_URL = "http://latchkey-self.invalid/permissions/self"
_GIT_SCOPE_PATTERN = re.compile(r"^github-git(:|$)")
_KINDS = ("api", "git-write")


class GitHubAccessError(Exception):
    """Raised when latchkey cannot be asked at all."""


class AccessUnknownError(Exception):
    """Raised when latchkey ran but brought back no readable permission answer."""


def _latchkey_curl(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["latchkey", "curl", *args], capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise GitHubAccessError(f"cannot run latchkey: {e}") from e


def is_api_permitted() -> bool:
    return _latchkey_curl("-sf", _API_PROBE_URL).returncode == 0


def grants_git_write(rules: object) -> bool:
    """Whether the gateway's permission rules let this agent push."""
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        for scope, permissions in rule.items():
            if not isinstance(scope, str) or not (
                _GIT_SCOPE_PATTERN.match(scope) or scope == "any"
            ):
                continue
            if isinstance(permissions, list) and (
                "github-git-write" in permissions or "any" in permissions
            ):
                return True
    return False


def is_git_write_permitted() -> bool:
    completed = _latchkey_curl("-s", _PERMISSIONS_URL)
    if completed.returncode != 0:
        raise AccessUnknownError(
            f"latchkey exited {completed.returncode}: {completed.stderr.strip()[:300]}"
        )
    try:
        answer = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise AccessUnknownError(f"unreadable answer: {completed.stdout[:300]}") from e
    if isinstance(answer, dict) and "rules" not in answer and "error" in answer:
        raise AccessUnknownError(str(answer["error"])[:300])
    return grants_git_write(answer.get("rules") if isinstance(answer, dict) else None)


_PROBES = {"api": is_api_permitted, "git-write": is_git_write_permitted}


class Access(Enum):
    PERMITTED = "permitted"
    NOT_PERMITTED = "NOT permitted"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class KindAccess:
    access: Access
    reason: str = ""

    def __str__(self) -> str:
        if self.reason:
            return f"{self.access.value} ({self.reason})"
        return self.access.value


def access_report(kinds: list[str]) -> dict[str, KindAccess]:
    """Each kind's access, with the gateway's reason when it is UNKNOWN."""
    report: dict[str, KindAccess] = {}
    for kind in kinds:
        try:
            is_permitted = _PROBES[kind]()
        except AccessUnknownError as e:
            report[kind] = KindAccess(Access.UNKNOWN, " ".join(str(e).split()))
            continue
        report[kind] = KindAccess(
            Access.PERMITTED if is_permitted else Access.NOT_PERMITTED
        )
    return report


def _is_all_permitted(report: dict[str, KindAccess]) -> bool:
    return all(status.access is Access.PERMITTED for status in report.values())


def _format(report: dict[str, KindAccess]) -> str:
    return "\n".join(f"{kind}: {status}" for kind, status in report.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe, and wait for, GitHub access through latchkey."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "wait"):
        subcommand = subcommands.add_parser(name)
        subcommand.add_argument("--need", action="append", choices=_KINDS)
    wait = subcommands.choices["wait"]
    wait.add_argument("--timeout-seconds", type=float, default=300.0)
    wait.add_argument("--interval-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    try:
        kinds = args.need or list(_KINDS)
        report = access_report(kinds)
        if args.command == "wait":
            deadline = time.monotonic() + args.timeout_seconds
            while not _is_all_permitted(report) and time.monotonic() < deadline:
                time.sleep(args.interval_seconds)
                report = access_report(kinds)
    except GitHubAccessError as e:
        print(f"github_access: {e}", file=sys.stderr)
        return 1

    if _is_all_permitted(report):
        print(_format(report))
        return 0
    if args.command == "wait":
        print(_format(report), file=sys.stderr)
        return 1
    print(_format(report))
    is_unknown = any(status.access is Access.UNKNOWN for status in report.values())
    return 2 if is_unknown else 1


if __name__ == "__main__":
    sys.exit(main())
