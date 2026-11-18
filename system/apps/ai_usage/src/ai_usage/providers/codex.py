"""Codex (OpenAI's coding agent, signed in with a ChatGPT plan): where it reports usage, and its layouts.

Not yet checked against a real reply: written from the Codex CLI's own source and from other
tools that read the same endpoint. The reply's limit windows come in a different order on
different plans, so each window is named by its length, not its position.

Studio gives every Codex chat one shared sign-in: each chat's own Codex folder links to the
same ``auth.json``. Its layout::

    {"tokens": {"access_token": "...", "account_id": "...", "id_token": "<JWT>", ...}, ...}
"""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_usage import provider as base
from ai_usage.provider import Limit, UsageProvider

SHARED_ACCOUNT = "chatgpt"
# Window lengths the reply reports, in seconds, and what ChatGPT's usage page calls them.
WINDOW_NAMES = {
    18000: ("five_hour", "5-hour window"),
    604800: ("weekly", "Weekly"),
    2592000: ("monthly", "Monthly"),
}


def window_limit(window: Any, prefix: str = "", label_prefix: str = "") -> Limit | None:
    if not isinstance(window, dict) or window.get("used_percent") is None:
        return None
    seconds = int(window.get("limit_window_seconds") or 0)
    key, label = WINDOW_NAMES.get(
        seconds, (f"window_{seconds}s", f"{round(seconds / 3600)}-hour window")
    )
    reset_at = window.get("reset_at") or window.get("resets_at")
    if isinstance(reset_at, (int, float)):
        resets_at: str | None = datetime.fromtimestamp(
            reset_at, timezone.utc
        ).isoformat()
    elif isinstance(reset_at, str):
        resets_at = reset_at
    elif window.get("reset_after_seconds") is not None:
        resets_at = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp()
            + float(window["reset_after_seconds"]),
            timezone.utc,
        ).isoformat()
    else:
        resets_at = None
    return Limit(
        key=prefix + key,
        label=label_prefix + (label.lower() if label_prefix else label),
        percent=float(window["used_percent"]),
        resets_at=resets_at,
    )


def windows_of(group: Any) -> list[Any]:
    """The windows of one rate-limit group, under either the current or the older field names."""
    if not isinstance(group, dict):
        return []
    return [
        group.get("primary_window", group.get("primary")),
        group.get("secondary_window", group.get("secondary")),
    ]


def parse_codex_limits(raw: dict[str, Any]) -> list[Limit]:
    """The limits Codex's usage reply reports: the plan's windows, then code review's."""
    limits: list[Limit] = []
    for window in windows_of(raw.get("rate_limit")):
        limit = window_limit(window)
        if limit is not None:
            limits.append(limit)
    for window in windows_of(raw.get("code_review_rate_limit")):
        limit = window_limit(
            window, prefix="code_review_", label_prefix="Code review, "
        )
        if limit is not None:
            limits.append(limit)
    return limits


def jwt_claims(token: Any) -> dict[str, Any]:
    """The claims inside a sign-in token, read without checking it (only to name the plan)."""
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def read_codex_plan(credentials: dict[str, Any]) -> str | None:
    """The ChatGPT plan named in the sign-in token, e.g. 'Plus' or 'Pro'."""
    claims = jwt_claims((credentials.get("tokens") or {}).get("id_token"))
    auth = claims.get("https://api.openai.com/auth") or {}
    plan = auth.get("chatgpt_plan_type") if isinstance(auth, dict) else None
    return str(plan).capitalize() if plan else None


def codex_account(account: str | None) -> str | None:
    """Every Codex chat here shares one ChatGPT sign-in, so they share one account."""
    return SHARED_ACCOUNT


def codex_credentials_paths(account: str) -> list[Path]:
    homes = [Path("/home/user"), Path.home()]
    return [
        base.ACCOUNTS_DIR / account / "auth.json",
        *(home / ".codex" / "auth.json" for home in homes),
    ]


def codex_headers(credentials: dict[str, Any]) -> dict[str, str]:
    # The account id picks which ChatGPT workspace's limits come back; without it the
    # reply can describe a different one.
    account_id = (credentials.get("tokens") or {}).get("account_id")
    return (
        {"ChatGPT-Account-Id": account_id}
        if isinstance(account_id, str) and account_id
        else {}
    )


PROVIDER = UsageProvider(
    agent_type="codex",
    label="ChatGPT (Codex)",
    usage_url="https://chatgpt.com/backend-api/wham/usage",
    usage_page="https://chatgpt.com/codex/settings/usage",
    account_key=codex_account,
    credentials_paths=codex_credentials_paths,
    token_path=("tokens", "access_token"),
    request_headers=codex_headers,
    parse_limits=parse_codex_limits,
    read_plan=read_codex_plan,
)
