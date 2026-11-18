"""Claude: where it reports usage, and how its usage reply and sign-in file are laid out."""

from pathlib import Path
from typing import Any

from ai_usage import provider as base
from ai_usage.provider import Limit, UsageProvider


def parse_claude_limits(raw: dict[str, Any]) -> list[Limit]:
    """The limits Claude's usage reply reports, in the order Claude's usage page lists them."""
    limits: list[Limit] = []
    named = (
        ("five_hour", "Current session"),
        ("seven_day", "Weekly, all models"),
        ("seven_day_opus", "Weekly, Opus only"),
        ("seven_day_sonnet", "Weekly, Sonnet only"),
    )
    for key, label in named:
        entry = raw.get(key)
        if isinstance(entry, dict) and entry.get("utilization") is not None:
            limits.append(
                Limit(
                    key=key,
                    label=label,
                    percent=float(entry["utilization"]),
                    resets_at=entry.get("resets_at"),
                )
            )
    for entry in raw.get("limits") or []:
        if (
            not isinstance(entry, dict)
            or entry.get("kind") != "weekly_scoped"
            or not entry.get("is_active")
        ):
            continue
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        if not model:
            continue
        limits.append(
            Limit(
                key="weekly_" + model.lower().replace(" ", "_"),
                label=f"Weekly, {model} only",
                percent=float(entry.get("percent") or 0),
                resets_at=entry.get("resets_at"),
            )
        )
    return limits


def read_claude_plan(credentials: dict[str, Any]) -> str | None:
    """The Claude plan named in Claude's sign-in file, e.g. 'Max 20x'."""
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    name = str(oauth.get("subscriptionType") or "").capitalize()
    tier = str(oauth.get("rateLimitTier") or "")
    for multiple in ("20x", "5x"):
        if multiple in tier:
            name += f" {multiple}"
            break
    return name or None


def claude_account(account: str | None) -> str | None:
    """Claude chats each sign in with the Studio account they are labelled with."""
    return account or None


def claude_credentials_paths(account: str) -> list[Path]:
    return [base.ACCOUNTS_DIR / account / ".credentials.json"]


def claude_headers(credentials: dict[str, Any]) -> dict[str, str]:
    return {"anthropic-beta": "oauth-2025-04-20"}


PROVIDER = UsageProvider(
    agent_type="claude",
    label="Claude",
    usage_url="https://api.anthropic.com/api/oauth/usage",
    usage_page="https://claude.ai/settings/usage",
    account_key=claude_account,
    credentials_paths=claude_credentials_paths,
    token_path=("claudeAiOauth", "accessToken"),
    request_headers=claude_headers,
    parse_limits=parse_claude_limits,
    read_plan=read_claude_plan,
)
