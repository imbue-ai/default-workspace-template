"""File GitHub issues for registered credentials nearing their expiry.

The registry is ``.github/credential-expiries.toml`` (see its header for the
entry schema): credentials that die silently at expiry -- tokens stored
inside third-party services, TLS certificates -- get a committed
``expires_on`` date there, and the weekly
``.github/workflows/credential-expiry-reminder.yml`` run files one GitHub
issue per credential once it is within the reminder window. Idempotent: an
open issue with the credential's exact title suppresses a duplicate.

Run from the repo root as a module (the workflow provides ``GH_TOKEN``)::

    python -m scripts.credential_expiry_reminder

Exit codes:
    0 -- ok (issues filed or nothing due)
    1 -- a malformed registry entry, or a gh call failed
"""

import json
import subprocess
import sys
import tomllib
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# The repo-relative spelling, for human-facing text (issue bodies, errors);
# reads go through REGISTRY_PATH, anchored at the repo root so the cwd never
# matters.
REGISTRY_DISPLAY_PATH: Final[str] = ".github/credential-expiries.toml"
REGISTRY_PATH: Final[Path] = _REPO_ROOT / REGISTRY_DISPLAY_PATH

REMINDER_WINDOW_DAYS: Final[int] = 30

# gh's default list size is 30; the exact-title dedupe scans open issues, so
# read enough of them that a busy repo cannot hide an existing reminder.
_OPEN_ISSUE_SCAN_LIMIT: Final[int] = 1000

_ISSUE_TITLE_PREFIX: Final[str] = "credential expiring: "


class CredentialExpiryReminderError(Exception):
    """Raised when the registry is malformed or a gh call fails."""


def issue_title_for_credential(credential_name: str) -> str:
    return f"{_ISSUE_TITLE_PREFIX}{credential_name}"


def parse_credential_registry(registry_text: str) -> list[dict[str, Any]]:
    """Parse the registry TOML, validating every entry's schema.

    Raises ``CredentialExpiryReminderError`` on any malformed entry: the
    registry is a committed config file, so a bad entry must fail the run
    loudly rather than silently dropping its reminder.
    """
    try:
        document = tomllib.loads(registry_text)
    except tomllib.TOMLDecodeError as exc:
        raise CredentialExpiryReminderError(f"cannot parse {REGISTRY_DISPLAY_PATH}: {exc}") from exc
    entries = document.get("credentials", [])
    if not isinstance(entries, list):
        raise CredentialExpiryReminderError(f"{REGISTRY_DISPLAY_PATH}: 'credentials' must be an array of tables")
    seen_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CredentialExpiryReminderError(
                f"{REGISTRY_DISPLAY_PATH}: every 'credentials' entry must be a [[credentials]] table"
            )
        name = entry.get("name")
        expires_on = entry.get("expires_on")
        rotation = entry.get("rotation")
        if not isinstance(name, str) or not name:
            raise CredentialExpiryReminderError(
                f"{REGISTRY_DISPLAY_PATH}: every entry needs a non-empty string 'name'"
            )
        # The open-issue dedupe keys on the exact title derived from the name,
        # so a duplicate name would silently suppress one entry's reminder.
        if name in seen_names:
            raise CredentialExpiryReminderError(f"{REGISTRY_DISPLAY_PATH}: duplicate credential name {name!r}")
        seen_names.add(name)
        # tomllib parses a TOML local date into datetime.date; a quoted string
        # (or a datetime) is a schema mistake worth failing on.
        if type(expires_on) is not date:
            raise CredentialExpiryReminderError(
                f"{REGISTRY_DISPLAY_PATH}: entry {name!r} needs 'expires_on' as a bare TOML date (e.g. 2027-08-24)"
            )
        if not isinstance(rotation, str) or not rotation:
            raise CredentialExpiryReminderError(
                f"{REGISTRY_DISPLAY_PATH}: entry {name!r} needs a non-empty string 'rotation'"
            )
    return entries


def credentials_needing_reminder(entries: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """The entries within the reminder window of their expiry (already-expired ones included)."""
    return [entry for entry in entries if entry["expires_on"] - timedelta(days=REMINDER_WINDOW_DAYS) <= today]


def build_issue_body(entry: dict[str, Any]) -> str:
    return (
        f"The credential `{entry['name']}` expires on **{entry['expires_on'].isoformat()}** "
        f"(registered in `{REGISTRY_DISPLAY_PATH}`; this issue is filed automatically once it is within "
        f"{REMINDER_WINDOW_DAYS} days).\n\n"
        f"Rotation: {entry['rotation']}\n\n"
        f"After rotating, update the entry's `expires_on` in `{REGISTRY_DISPLAY_PATH}` to the new date "
        "and close this issue."
    )


def _run_gh(arguments: list[str]) -> str:
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        raise CredentialExpiryReminderError(f"gh {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout


def _open_issue_titles() -> set[str]:
    stdout = _run_gh(["issue", "list", "--state", "open", "--limit", str(_OPEN_ISSUE_SCAN_LIMIT), "--json", "title"])
    try:
        issues = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CredentialExpiryReminderError(f"gh issue list returned unparseable JSON: {exc}") from exc
    return {issue["title"] for issue in issues}


def main() -> int:
    entries = parse_credential_registry(REGISTRY_PATH.read_text())
    due_entries = credentials_needing_reminder(entries, date.today())
    if not due_entries:
        print(f"No registered credential is within {REMINDER_WINDOW_DAYS} days of expiry.")
        return 0
    open_titles = _open_issue_titles()
    for entry in due_entries:
        title = issue_title_for_credential(entry["name"])
        if title in open_titles:
            print(f"Skipping {entry['name']!r}: an open issue already reminds about it.")
            continue
        _run_gh(["issue", "create", "--title", title, "--body", build_issue_body(entry)])
        print(f"Filed an expiry reminder issue for {entry['name']!r} (expires {entry['expires_on'].isoformat()}).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CredentialExpiryReminderError as exc:
        print(f"credential-expiry reminder failed: {exc}", file=sys.stderr)
        sys.exit(1)
