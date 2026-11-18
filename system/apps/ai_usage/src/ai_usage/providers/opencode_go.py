"""OpenCode Go (a subscription to a selection of open models): where it reports usage, and its layouts.

Not yet checked against a real reply: written from two independent open-source trackers that
read the same endpoint with the same key, and agree on its shape::

    {"usage": {"rolling": {"status": "ok", "percent": 4, "resetsAt": "<ISO 8601>"},
               "weekly":  {...}, "monthly": {...}}}

Studio runs OpenCode Go chats on pi, the same agent type as OpenRouter and pasted-key chats,
so a pi chat counts here only when its account is an OpenCode Go one. The pasted key sits in
that account's ``auth.json``::

    {"opencode-go": {"type": "api_key", "key": "..."}}
"""

import json
from pathlib import Path
from typing import Any

from ai_usage import provider as base
from ai_usage.provider import Limit, UsageProvider

LANE_ID = "opencode-go"
# The reply's windows, in the order OpenCode's Go page lists them, and what it calls them.
WINDOWS = (
    ("rolling", "5-hour window"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
)


def parse_opencode_go_limits(raw: dict[str, Any]) -> list[Limit]:
    """The limits OpenCode Go's usage reply reports: its 5-hour, weekly, and monthly windows."""
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return []
    limits: list[Limit] = []
    for key, label in WINDOWS:
        window = usage.get(key)
        if not isinstance(window, dict) or not isinstance(
            window.get("percent"), (int, float)
        ):
            continue
        resets_at = window.get("resetsAt")
        limits.append(
            Limit(
                key=key,
                label=label,
                percent=float(window["percent"]),
                resets_at=resets_at if isinstance(resets_at, str) else None,
            )
        )
    return limits


def account_lanes() -> dict[str, str]:
    """Which kind of sign-in each Studio account is, by account id."""
    try:
        index = json.loads((base.ACCOUNTS_DIR / "index.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        a["id"]: str(a.get("lane") or "")
        for a in index.get("accounts", [])
        if isinstance(a, dict) and "id" in a
    }


def opencode_go_account(account: str | None) -> str | None:
    """A pi chat's usage is OpenCode Go's only when the account it is labelled with is an OpenCode Go sign-in."""
    if not account or account_lanes().get(account) != LANE_ID:
        return None
    return account


def opencode_go_credentials_paths(account: str) -> list[Path]:
    return [base.ACCOUNTS_DIR / account / "auth.json"]


def opencode_go_headers(credentials: dict[str, Any]) -> dict[str, str]:
    return {"Accept": "application/json"}


PROVIDER = UsageProvider(
    agent_type="pi-coding",
    label="OpenCode Go",
    usage_url="https://opencode.ai/zen/go/v1/usage",
    usage_page="https://opencode.ai/auth",
    account_key=opencode_go_account,
    credentials_paths=opencode_go_credentials_paths,
    token_path=("opencode-go", "key"),
    request_headers=opencode_go_headers,
    parse_limits=parse_opencode_go_limits,
    read_plan=base.no_plan,
)
