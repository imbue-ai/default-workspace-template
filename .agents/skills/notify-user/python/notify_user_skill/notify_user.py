"""Post one notification from this chat into the Imbue Studio app's notification feed.

The message goes to the app's per-agent notifications route through the
latchkey gateway's ``minds-api-proxy`` (the gateway injects the app's API key;
this script never holds one), addressed as the current runtime agent for
authorization. The app reads that agent's chat_id label for the stable
conversation destination, including after a chat moves between agents.

Before posting it asks the chat app which pages are watching this chat (shown
and focused, ``GET /api/chats/<chat id>/watchers``) and sends their instance
ids as ``watched_by``, so the app can show nothing to a user who is already
reading the chat. When that question cannot be answered in two seconds the
notification goes out without the field, with a warning on stderr.

Exit codes:
    0  -- the app accepted the notification
    1  -- it did not (no gateway env, the app unreachable, a refusal); the
          reason is on stderr so the agent can tell the user the notification
          did not go out

Usage:
    uv run --no-sync .agents/skills/notify-user/scripts/notify_user.py [--title TITLE] MESSAGE

Environment:
    MNGR_AGENT_ID              Current runtime agent id, registered with the
                              gateway. MINDS_CHAT_ID identifies a conversation,
                              not the agent authorized to call this route.
    MINDS_CHAT_ID              The chat whose watchers are asked for; an agent
                              that is its own chat may lack it, and then
                              MNGR_AGENT_ID stands in.
    MINDS_APPS_FILE            The app registry the chat app's address is read
                              from (default data/.state/apps.toml).
    LATCHKEY_GATEWAY,           Gateway address + password mngr injects into the
    LATCHKEY_GATEWAY_PASSWORD   agent environment. Both must be present.
    LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE
                                The per-agent authorization JWT, forwarded when
                                set (a desktop-hosted gateway needs it; a
                                VPS-hosted one never injects it).

The gateway is addressed directly with the same headers the in-app callers send
rather than through ``latchkey curl`` (the ``minds-api`` skill's usual route), so
the script does not depend on the latchkey CLI being on the PATH of whatever tool
environment runs it.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from workspace_bare_scripts import message_chat

ENV_AGENT_ID = "MNGR_AGENT_ID"
ENV_CHAT_ID = "MINDS_CHAT_ID"
ENV_GATEWAY = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"

# One round trip to the desktop app; a wedged app must not stall the agent's
# turn.
_TIMEOUT_SECONDS = 10.0

# The watcher question is a courtesy: the notification goes out without its
# answer rather than wait on a slow chat app.
_WATCHERS_TIMEOUT_SECONDS = 2.0

# This module sits five directories below the repo root
# (.agents/skills/notify-user/python/notify_user_skill/).
_REPO_ROOT = Path(__file__).resolve().parents[5]


class HttpClient:
    """Indirection over the outbound HTTP so tests can intercept it."""

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        """POST a JSON body; return ``(status, response text)``, status ``None`` if unreachable."""
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            return None, str(exc)

    def get(self, url: str, timeout: float) -> tuple[int | None, str]:
        """GET a URL; return ``(status, response text)``, status ``None`` if unreachable, too slow, or garbled."""
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "replace")
        except (
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
            ValueError,
        ) as exc:
            return None, str(exc)


class WatchersUnknownError(Exception):
    """The chat app could not say who is watching this chat."""


def watchers_of_chat(http: HttpClient, environ: dict[str, str]) -> list[str]:
    """The instance ids of the pages watching this chat; raises WatchersUnknownError naming why they are unknown."""
    chat_id = environ.get(ENV_CHAT_ID, "") or environ.get(ENV_AGENT_ID, "")
    if not chat_id:
        raise WatchersUnknownError(f"{ENV_CHAT_ID} is not set")
    status, text = http.get(
        f"{message_chat.chat_app_url(environ, _REPO_ROOT)}/api/chats/{chat_id}/watchers",
        timeout=_WATCHERS_TIMEOUT_SECONDS,
    )
    if status is None:
        raise WatchersUnknownError(f"the chat app did not answer: {text}")
    if status != 200:
        raise WatchersUnknownError(f"the chat app answered {status}")
    try:
        watched_by = json.loads(text).get("watched_by")
    except (ValueError, AttributeError) as exc:
        raise WatchersUnknownError("the chat app's answer is not JSON") from exc
    if not isinstance(watched_by, list) or not all(
        isinstance(item, str) for item in watched_by
    ):
        raise WatchersUnknownError("the chat app's answer carries no list of watchers")
    return watched_by


def notify(
    message: str,
    title: str | None,
    *,
    http: HttpClient,
    environ: dict[str, str],
) -> bool:
    """Post the notification; return whether the app accepted it, reporting any failure on stderr."""
    if not message.strip():
        sys.stderr.write("notify-user: the message is empty; nothing was sent.\n")
        return False
    gateway = environ.get(ENV_GATEWAY, "")
    password = environ.get(ENV_GATEWAY_PASSWORD, "")
    if not gateway or not password:
        sys.stderr.write(
            "notify-user: the latchkey gateway env is not set, so the Imbue Studio app cannot be reached; "
            "the notification did not go out.\n"
        )
        return False
    agent_id = environ.get(ENV_AGENT_ID, "")
    if not agent_id:
        sys.stderr.write(
            f"notify-user: {ENV_AGENT_ID} is not set, so the sending agent cannot be "
            "identified; the notification did not go out.\n"
        )
        return False
    headers = {
        "Content-Type": "application/json",
        "X-Latchkey-Gateway-Password": password,
    }
    # Forwarded only when set, like every other gateway caller in this repo:
    # a desktop-hosted gateway needs it as the per-agent authorization JWT,
    # a VPS-hosted one never injects it.
    permissions = environ.get(ENV_GATEWAY_PERMISSIONS, "")
    if permissions:
        headers["X-Latchkey-Gateway-Permissions-Override"] = permissions
    payload: dict[str, str | list[str]] = {"message": message.strip()}
    if title:
        payload["title"] = title.strip()
    try:
        payload["watched_by"] = watchers_of_chat(http, environ)
    except WatchersUnknownError as exc:
        sys.stderr.write(
            f"notify-user: could not check who is watching this chat ({exc}); sending anyway\n"
        )
    status, text = http.post_json(
        f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/agents/{agent_id}/notifications",
        payload,
        headers,
        timeout=_TIMEOUT_SECONDS,
    )
    if status == 200:
        return True
    if status is None:
        sys.stderr.write(
            f"notify-user: the Imbue Studio app was unreachable ({text}); the notification did not go out.\n"
        )
    else:
        sys.stderr.write(
            f"notify-user: the Imbue Studio app answered {status} ({text.strip()[:200]}); the notification "
            "did not go out.\n"
        )
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Notify the user from this chat through the Imbue Studio app."
    )
    parser.add_argument(
        "--title", default=None, help="Optional title, shown as a prefix on the message"
    )
    parser.add_argument("message", help="One sentence summarizing what was done")
    args = parser.parse_args(argv)
    is_accepted = notify(
        args.message, args.title, http=HttpClient(), environ=dict(os.environ)
    )
    if is_accepted:
        sys.stderr.write("notify-user: notification sent.\n")
    return 0 if is_accepted else 1
