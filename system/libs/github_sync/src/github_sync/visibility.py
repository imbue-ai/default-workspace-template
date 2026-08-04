"""Sync-repo visibility checking through latchkey.

Public repos must never be synced to: agents might push secrets or other
sensitive data without thinking about it. The skill verifies the repo is
private at enable time, and the service re-checks periodically: pushes are
held until visibility is first confirmed private and halted whenever the
repo is confirmed public, while a re-check that fails outright keeps the
last confirmed answer (see runner._refresh_visibility for that policy).
"""

import json
import subprocess

from loguru import logger

from github_sync.config import parse_owner_and_name

VISIBILITY_PRIVATE = "private"
VISIBILITY_PUBLIC = "public"
VISIBILITY_UNKNOWN = "unknown"

_LATCHKEY_CURL_TIMEOUT_SECONDS = 60


def parse_visibility_response(body: str) -> str:
    """Map a GitHub `GET /repos/<owner>/<repo>` response body to a visibility.

    Anything that is not an explicit `"private": true/false` (error bodies,
    truncated output, a 404 for a deleted repo) is UNKNOWN, which callers
    treat as push-blocking.
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return VISIBILITY_UNKNOWN
    if not isinstance(data, dict):
        return VISIBILITY_UNKNOWN
    is_private = data.get("private")
    if is_private is True:
        return VISIBILITY_PRIVATE
    elif is_private is False:
        return VISIBILITY_PUBLIC
    else:
        return VISIBILITY_UNKNOWN


def check_repo_visibility(repo_url: str) -> str:
    """Ask GitHub (via latchkey) whether the sync repo is private.

    Goes through the one latchkey gateway; there is no fallback gateway, so a
    call that fails or returns an unparseable body is UNKNOWN and callers
    retry on the next tick.
    """
    owner, name = parse_owner_and_name(repo_url)
    api_url = f"https://api.github.com/repos/{owner}/{name}"
    # latchkey curl injects the GitHub credential server-side; -s keeps
    # stdout parseable.
    try:
        result = subprocess.run(
            ["latchkey", "curl", "-s", api_url],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LATCHKEY_CURL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("latchkey curl failed: {}", e)
        return VISIBILITY_UNKNOWN
    if result.returncode != 0:
        logger.debug(
            "latchkey curl exited {}: {}", result.returncode, result.stderr.strip()
        )
        return VISIBILITY_UNKNOWN
    return parse_visibility_response(result.stdout)
