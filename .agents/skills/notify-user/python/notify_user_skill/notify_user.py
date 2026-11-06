"""Post one notification from this chat into the Imbue Studio app's notification feed, and to the owner's devices
that turned notifications on.

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

Unless a page is watching the chat, the same message also goes to every device
the owner turned notifications on for (a phone with the workspace on its home
screen, a browser): one POST to the workspace shell's loopback
``/api/web-push/send``, whose Web Push reaches a device with the Imbue Studio
app closed. Tapping it opens the chat. A workspace with no such device answers
that nothing was sent, which is not a failure.

Exit codes:
    0  -- the app, or at least one device, accepted the notification
    1  -- neither did (no gateway env, the app unreachable, a refusal, and no
          device to push to); the reason is on stderr so the agent can tell the
          user the notification did not go out

Usage:
    python3 .agents/skills/notify-user/python/notify_user_skill/notify_user.py [--title TITLE] MESSAGE

It imports only the standard library and the bare ``workspace_bare_scripts``, so it runs
under the system ``python3`` with no venv.

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
    MINDS_WORKSPACE_SERVER_URL  The workspace shell's address (default
                                http://127.0.0.1:8000), which pushes to devices.

The gateway is addressed directly with the same headers the in-app callers send
rather than through ``latchkey curl`` (the ``imbue-studio`` skill's usual route), so
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
import urllib.parse
import urllib.request
from pathlib import Path

# Run by its path, the module puts the bare packages' directory on sys.path; imported as
# part of its package, the venv already has it.
if not __package__:
    # import-path: no-venv -- its only import beyond the standard library is `workspace_bare_scripts`
    sys.path.insert(0, str(Path(__file__).resolve().parents[5] / "system" / "scripts"))

from workspace_bare_scripts import message_chat

ENV_AGENT_ID = "MNGR_AGENT_ID"
ENV_CHAT_ID = "MINDS_CHAT_ID"
ENV_GATEWAY = "LATCHKEY_GATEWAY"
ENV_GATEWAY_PASSWORD = "LATCHKEY_GATEWAY_PASSWORD"
ENV_GATEWAY_PERMISSIONS = "LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"
ENV_SHELL_URL = "MINDS_WORKSPACE_SERVER_URL"

DEFAULT_SHELL_URL = "http://127.0.0.1:8000"
WEB_PUSH_SEND_PATH = "/api/web-push/send"

# One round trip to the desktop app; a wedged app must not stall the agent's
# turn.
_TIMEOUT_SECONDS = 10.0

# The watcher question is a courtesy: the notification goes out without its
# answer rather than wait on a slow chat app.
_WATCHERS_TIMEOUT_SECONDS = 2.0

# The shell posts to every device at once, each under its own shorter timeout,
# so this covers a slow push service with room to spare.
_PUSH_TIMEOUT_SECONDS = 12.0

# The longest title and body the shell's send route takes; a longer message
# without a title is cut for the title and carried in the body, itself cut at
# its own bound.
_PUSH_TITLE_MAX_LENGTH = 120
_PUSH_BODY_MAX_LENGTH = 1000
# The shell also refuses a notification whose JSON, as its service worker reads
# it, is over this many UTF-8 bytes; a body of emoji fits the character bound
# but not this one, so the body is cut further until it fits.
_PUSH_PAYLOAD_MAX_BYTES = 3993

# This module sits five directories below the repo root
# (.agents/skills/notify-user/python/notify_user_skill/).
_REPO_ROOT = Path(__file__).resolve().parents[5]


class HttpClient:
    """Indirection over the outbound HTTP so tests can intercept it."""

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        """POST a JSON body; return ``(status, response text)``, status ``None`` if unreachable, too slow, or garbled."""
        body = json.dumps(payload).encode("utf-8")
        try:
            request = urllib.request.Request(
                url, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
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


def _chat_id(environ: dict[str, str]) -> str:
    """This chat's id ("" when unknown): the chat's own, else the agent's, which a chat that never moved shares."""
    return environ.get(ENV_CHAT_ID, "") or environ.get(ENV_AGENT_ID, "")


def watchers_of_chat(http: HttpClient, environ: dict[str, str]) -> list[str]:
    """The instance ids of the pages watching this chat; raises WatchersUnknownError naming why they are unknown."""
    chat_id = _chat_id(environ)
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


def _watched_by_or_none(http: HttpClient, environ: dict[str, str]) -> list[str] | None:
    """The pages watching this chat, or None (warned on stderr) when that cannot be told."""
    try:
        return watchers_of_chat(http, environ)
    except WatchersUnknownError as exc:
        sys.stderr.write(
            f"notify-user: could not check who is watching this chat ({exc}); sending anyway\n"
        )
        return None


def post_to_studio_app(
    message: str,
    title: str | None,
    watched_by: list[str] | None,
    *,
    http: HttpClient,
    environ: dict[str, str],
) -> bool:
    """Post the notification to the Imbue Studio app; return whether it accepted it, reporting any failure on stderr."""
    gateway = environ.get(ENV_GATEWAY, "")
    password = environ.get(ENV_GATEWAY_PASSWORD, "")
    if not gateway or not password:
        sys.stderr.write(
            "notify-user: the latchkey gateway env is not set, so the Imbue Studio app cannot be reached.\n"
        )
        return False
    agent_id = environ.get(ENV_AGENT_ID, "")
    if not agent_id:
        sys.stderr.write(
            f"notify-user: {ENV_AGENT_ID} is not set, so the sending agent cannot be "
            "identified to the Imbue Studio app.\n"
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
    payload: dict[str, str | list[str]] = {"message": message}
    if title:
        payload["title"] = title.strip()
    if watched_by is not None:
        payload["watched_by"] = watched_by
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
            f"notify-user: the Imbue Studio app was unreachable ({text}).\n"
        )
    else:
        sys.stderr.write(
            f"notify-user: the Imbue Studio app answered {status} ({text.strip()[:200]}).\n"
        )
    return False


def _cut(text: str, limit: int) -> str:
    """``text`` when it fits in ``limit`` characters, else its start and an ellipsis within them."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _utf8_text(text: str) -> str:
    """``text`` with what UTF-8 cannot carry (the lone surrogates argv decodes bytes that are not UTF-8 into) shown as
    ``?``, so the device notification's size can be measured and the shell can send it."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _payload_size(notification: dict[str, str]) -> int:
    """The notification's size as the shell sends it: its JSON in plain UTF-8, an absent tag as null."""
    sent = {
        "title": notification["title"],
        "body": notification["body"],
        "path": notification["path"],
        "tag": notification.get("tag"),
    }
    return len(json.dumps(sent, ensure_ascii=False).encode("utf-8"))


def device_notification(
    message: str, title: str | None, environ: dict[str, str]
) -> dict[str, str]:
    """The notification the devices show: the title over the message, or the message as the title (cut, and then
    carried in the body) when there is none, each cut to what the shell takes, the body further until the whole fits
    in one push message; tapping it opens this chat."""
    chat_id = _chat_id(environ)
    path = (
        "/?" + urllib.parse.urlencode({"open": f"chat:/?chat={chat_id}"})
        if chat_id
        else "/"
    )
    message = _utf8_text(message)
    stripped_title = _utf8_text((title or "").strip())
    if stripped_title:
        heading, body = stripped_title, message
    else:
        heading = message
        body = "" if len(message) <= _PUSH_TITLE_MAX_LENGTH else message
    notification = {
        "title": _cut(heading, _PUSH_TITLE_MAX_LENGTH),
        "body": _cut(body, _PUSH_BODY_MAX_LENGTH),
        "path": path,
    }
    # One notification per chat on a device: a newer one from the same chat replaces it.
    if chat_id:
        notification["tag"] = f"chat-{chat_id}"
    body_limit = _PUSH_BODY_MAX_LENGTH
    while (
        notification["body"] and _payload_size(notification) > _PUSH_PAYLOAD_MAX_BYTES
    ):
        body_limit = body_limit * 9 // 10
        notification["body"] = _cut(body, body_limit) if body_limit > 3 else ""
    return notification


def push_to_devices(
    message: str,
    title: str | None,
    watched_by: list[str] | None,
    *,
    http: HttpClient,
    environ: dict[str, str],
) -> int:
    """Push the notification to the owner's registered devices through the shell; return how many push services
    took it. Skipped (0) while a page is watching the chat. A device the shell could not reach is reported on
    stderr; a workspace with no registered device sends nothing and says nothing."""
    if watched_by:
        return 0
    shell_url = (environ.get(ENV_SHELL_URL) or DEFAULT_SHELL_URL).rstrip("/")
    status, text = http.post_json(
        f"{shell_url}{WEB_PUSH_SEND_PATH}",
        device_notification(message, title, environ),
        {"Content-Type": "application/json"},
        timeout=_PUSH_TIMEOUT_SECONDS,
    )
    if status != 200:
        reason = (
            f"was unreachable: {text}"
            if status is None
            else f"answered {status}: {text.strip()[:200]}"
        )
        sys.stderr.write(
            f"notify-user: could not push the notification to your devices (the workspace {reason}).\n"
        )
        return 0
    try:
        outcome = json.loads(text)
        deliveries = outcome["deliveries"]
        accepted_count = int(outcome["accepted_count"])
    except (ValueError, KeyError, TypeError):
        sys.stderr.write(
            "notify-user: the workspace's answer about your devices is not readable.\n"
        )
        return 0
    if deliveries and accepted_count == 0:
        sys.stderr.write(
            f"notify-user: none of your {len(deliveries)} device(s) took the notification.\n"
        )
    return accepted_count


def notify(
    message: str,
    title: str | None,
    *,
    http: HttpClient,
    environ: dict[str, str],
) -> bool:
    """Notify the user in the Imbue Studio app and on their devices; return whether either took it, reporting any
    failure on stderr."""
    stripped = message.strip()
    if not stripped:
        sys.stderr.write("notify-user: the message is empty; nothing was sent.\n")
        return False
    watched_by = _watched_by_or_none(http, environ)
    is_app_accepted = post_to_studio_app(
        stripped, title, watched_by, http=http, environ=environ
    )
    pushed_count = push_to_devices(
        stripped, title, watched_by, http=http, environ=environ
    )
    if pushed_count:
        sys.stderr.write(
            f"notify-user: also pushed to {pushed_count} of your devices.\n"
            if is_app_accepted
            else f"notify-user: pushed to {pushed_count} of your devices, though not to the Imbue Studio app.\n"
        )
    is_sent = is_app_accepted or pushed_count > 0
    if not is_sent:
        sys.stderr.write("notify-user: the notification did not go out.\n")
    return is_sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Notify the user from this chat in the Imbue Studio app and on their devices."
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional title: a prefix on the message in the app, its heading on a device",
    )
    parser.add_argument("message", help="One sentence summarizing what was done")
    args = parser.parse_args(argv)
    is_sent = notify(
        args.message, args.title, http=HttpClient(), environ=dict(os.environ)
    )
    if is_sent:
        sys.stderr.write("notify-user: notification sent.\n")
    return 0 if is_sent else 1


if __name__ == "__main__":
    sys.exit(main())
