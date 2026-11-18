"""Antigravity (Google's coding agent): where it reports usage, and how its reply and sign-in file are laid out.

In Studio, Antigravity stores its OAuth token in
``.gemini/antigravity-cli/antigravity-oauth-token`` under the signed-in Google
account's folder (or the user's home directory)::

    {"token": {"access_token": "...", "refresh_token": "...", "expiry": "..."}, "auth_method": "consumer", ...}

Two endpoints are read. The quota endpoint reports ``remainingFraction`` (0.0 to 1.0)
per model, with Gemini models sharing one pool and Claude/GPT models sharing another; it
knows only each pool's 5-hour limit. The quota summary endpoint reports each group of
models' weekly and 5-hour limits, but lists only the groups Google chooses to (for a
consumer sign-in, the Claude and GPT models). A reading keeps the first reply as it came
and the second under ``quotaSummary``; a pool the summary does not list is shown from the
first.
"""

from pathlib import Path
from typing import Any

from ai_usage import provider as base
from ai_usage.provider import Limit, UsageProvider

SHARED_ACCOUNT = "google"
TOKEN_RELATIVE_PATH = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
INTERNAL_MODEL_PREFIXES = ("chat_", "tab_")
MODEL_POOLS = (
    ("gemini", "Gemini models", ("gemini-",)),
    ("claude_gpt", "Claude and GPT models", ("claude-", "gpt-")),
)
# What the summary calls each limit's window, and what the page calls it.
WINDOW_LABELS = {"weekly": "weekly", "5h": "5-hour"}
# The window of the one limit the quota endpoint reports per pool.
POOL_WINDOW_LABEL = "5-hour"
SUMMARY_KEY = "quotaSummary"


def used_percent(remaining_fraction: Any) -> float | None:
    """Turns Antigravity's remainingFraction (0.0-1.0) into percent used (0-100)."""
    if not isinstance(remaining_fraction, (int, float)):
        return None
    clamped = max(0.0, min(1.0, float(remaining_fraction)))
    return round((1.0 - clamped) * 100.0, 2)


def bucket_model_id(bucket: dict[str, Any]) -> str:
    model = bucket.get("model")
    if isinstance(model, dict) and isinstance(model.get("modelId"), str):
        return model["modelId"]
    model_id = bucket.get("modelId")
    return model_id if isinstance(model_id, str) else ""


def parse_bucket_pools(buckets: list[Any]) -> list[Limit]:
    """Combines per-model quota buckets into the shared model pools Antigravity enforces."""
    pool_used: dict[str, float] = {}
    pool_resets: dict[str, str | None] = {}
    other_limits: list[Limit] = []

    for entry in buckets:
        if not isinstance(entry, dict) or entry.get("disabled"):
            continue
        percent = used_percent(entry.get("remainingFraction"))
        if percent is None:
            continue
        model_id = bucket_model_id(entry)
        resets_at = (
            entry.get("resetTime") if isinstance(entry.get("resetTime"), str) else None
        )
        if model_id.startswith(INTERNAL_MODEL_PREFIXES) and resets_at is None:
            continue

        matched_pool = None
        for pool_key, _pool_label, prefixes in MODEL_POOLS:
            if model_id.startswith(prefixes):
                matched_pool = pool_key
                break

        if matched_pool is not None:
            if matched_pool not in pool_used or percent > pool_used[matched_pool]:
                pool_used[matched_pool] = percent
            if resets_at and (
                pool_resets.get(matched_pool) is None
                or resets_at < str(pool_resets[matched_pool])
            ):
                pool_resets[matched_pool] = resets_at
        elif model_id:
            model = entry.get("model")
            label = (
                model.get("displayName")
                if isinstance(model, dict) and isinstance(model.get("displayName"), str)
                else model_id
            )
            other_limits.append(
                Limit(key=model_id, label=label, percent=percent, resets_at=resets_at)
            )

    limits: list[Limit] = []
    for pool_key, pool_label, _prefixes in MODEL_POOLS:
        if pool_key in pool_used:
            limits.append(
                Limit(
                    key=pool_key,
                    label=pool_label,
                    percent=pool_used[pool_key],
                    resets_at=pool_resets.get(pool_key),
                )
            )
    limits.extend(other_limits)
    return limits


def parse_summary_groups(groups: list[Any]) -> list[Limit]:
    """Reads limits from a quotaSummary/retrieveUserQuotaSummary group list."""
    limits: list[Limit] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("displayName") or "").strip()
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            percent = used_percent(bucket.get("remainingFraction"))
            if percent is None:
                continue
            bucket_id = str(bucket.get("bucketId") or "").strip()
            window = str(
                bucket.get("window")
                or bucket.get("refreshWindowLabel")
                or bucket.get("displayName")
                or bucket_id
            ).strip()
            key = (
                bucket_id.replace("-", "_")
                if bucket_id
                else window.lower().replace(" ", "_")
            )
            window_label = WINDOW_LABELS.get(window.lower(), window)
            label = (
                f"{group_name}, {window_label}"
                if group_name and window_label
                else (group_name or window_label or key)
            )
            resets_at = (
                bucket.get("resetTime")
                if isinstance(bucket.get("resetTime"), str)
                else None
            )
            limits.append(
                Limit(key=key, label=label, percent=percent, resets_at=resets_at)
            )
    return limits


def summary_groups(raw: dict[str, Any]) -> list[Any]:
    summary = raw.get(SUMMARY_KEY)
    groups = summary.get("groups") if isinstance(summary, dict) else raw.get("groups")
    return groups if isinstance(groups, list) else []


def pools_in_summary(groups: list[Any]) -> set[str]:
    """The model pools whose limits the summary lists, matched by the group's name."""
    names = {
        str(group.get("displayName") or "").strip().lower()
        for group in groups
        if isinstance(group, dict)
    }
    return {
        pool_key for pool_key, pool_label, _prefixes in MODEL_POOLS if pool_label.lower() in names
    }


def parse_antigravity_limits(raw: dict[str, Any]) -> list[Limit]:
    """The limits Antigravity's replies report, inverting remainingFraction to percent used.

    Each group the summary lists gives its weekly and 5-hour limits; a pool it does not
    list gives the one 5-hour limit the quota endpoint reports for it.
    """
    groups = summary_groups(raw)
    limits = parse_summary_groups(groups)
    listed = pools_in_summary(groups) if limits else set()

    buckets = raw.get("buckets")
    models = raw.get("models")
    if not isinstance(buckets, list) and isinstance(models, dict):
        buckets = [
            {
                "modelId": model_id,
                "model": {"modelId": model_id, "displayName": info.get("displayName")},
                **(info.get("quotaInfo") or {}),
            }
            for model_id, info in models.items()
            if isinstance(info, dict)
            and not info.get("isInternal")
            and isinstance(info.get("quotaInfo"), dict)
        ]
    if isinstance(buckets, list):
        for limit in parse_bucket_pools(buckets):
            if limit.key in listed:
                continue
            if limits and limit.key in {key for key, _label, _prefixes in MODEL_POOLS}:
                # Beside limits named by their window, say which window this one is.
                limit = limit.model_copy(
                    update={"label": f"{limit.label}, {POOL_WINDOW_LABEL}"}
                )
            limits.append(limit)
    return limits


def antigravity_account(account: str | None) -> str | None:
    """Antigravity chats sign in with the Google account Studio labels them with, or one shared sign-in."""
    return account or SHARED_ACCOUNT


def antigravity_credentials_paths(account: str) -> list[Path]:
    homes = [Path("/home/user"), Path.home()]
    return [
        base.ACCOUNTS_DIR / account / TOKEN_RELATIVE_PATH,
        *(home / TOKEN_RELATIVE_PATH for home in homes),
    ]


def antigravity_headers(credentials: dict[str, Any]) -> dict[str, str]:
    return {"Content-Type": "application/json", "User-Agent": "antigravity"}


PROVIDER = UsageProvider(
    agent_type="antigravity",
    label="Antigravity",
    usage_url="https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
    usage_page="https://antigravity.google/",
    account_key=antigravity_account,
    credentials_paths=antigravity_credentials_paths,
    token_path=("token", "access_token"),
    request_headers=antigravity_headers,
    request_body=b"{}",
    extra_usage_urls={
        SUMMARY_KEY: "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
    },
    parse_limits=parse_antigravity_limits,
    read_plan=base.no_plan,
)
