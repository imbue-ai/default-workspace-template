"""Post one notification from this chat into the Imbue Studio app's notification feed, and to the owner's devices
that turned notifications on.

The message goes to the app's per-agent notifications route through the
latchkey gateway's ``minds-api-proxy`` (the gateway injects the app's API key;
this script never holds one), addressed as the current runtime agent for
authorization. The app reads that agent's chat_id label for the stable
conversation destination, including after a chat moves between agents.

It first tells the chat app about the notification
(``POST /api/chats/<chat id>/notification``), which shows it as a bubble over
the agent on the workspace's desktop for everyone not looking at this chat,
whether or not the Imbue Studio app takes it; when the chat app cannot be told
in two seconds, a warning goes to stderr and nothing else changes.

Before posting it asks the chat app which pages are watching this chat (shown
and focused, ``GET /api/chats/<chat id>/watchers``) and sends their instance
ids as ``watched_by``, so the app can show nothing to a user who is already
reading the chat. When that question cannot be answered in two seconds the
notification goes out without the field, with a warning on stderr.

The same answer says whether the chat is the owner's own (nobody but the owner
has written in it) and which of the watching pages are the owner's. For the
owner's own chat, while none of the owner's pages is watching it, the message
also goes to every device the owner turned notifications on for (a phone with
the workspace on its home screen, a browser): one POST to the workspace shell's
loopback ``/api/web-push/send``, carrying the shell's send token (read from the
shell's state; app code is never given it). The shell names the chat as the
notification's title, shows the message itself only if the owner turned full
text on, folds a burst from one chat into one notification, and drops a send
past its limits. Tapping it opens the chat. A chat someone else wrote in, or
one whose owner cannot be told, pushes nothing. A workspace with no such device
answers that nothing was sent, which is not a failure.

Exit codes:
    0  -- the app accepted the notification, or the shell queued it for the
          owner's devices
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
    MINDS_CHAT_ID              The chat the notification is from, whose watchers
                              are asked for; an agent that is its own chat may
                              lack it, and then MNGR_AGENT_ID stands in.
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
    MINDS_WEB_PUSH_SEND_TOKEN_FILE
                                The shell's send token (default
                                data/.state/system_interface/web_push/send_token).

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
ENV_SEND_TOKEN_FILE = "MINDS_WEB_PUSH_SEND_TOKEN_FILE"

DEFAULT_SHELL_URL = "http://127.0.0.1:8000"
WEB_PUSH_SEND_PATH = "/api/web-push/send"
SEND_TOKEN_HEADER = "X-Web-Push-Token"
DEFAULT_SEND_TOKEN_PATH = Path("data/.state/system_interface/web_push/send_token")

# One round trip to the desktop app; a wedged app must not stall the agent's
# turn.
_TIMEOUT_SECONDS = 10.0

# The watcher question and the desktop bubble are courtesies: the notification
# goes out without them rather than wait on a slow chat app.
_CHAT_APP_TIMEOUT_SECONDS = 2.0

# The shell queues the push and answers at once; posting happens on its own
# thread.
_PUSH_TIMEOUT_SECONDS = 5.0

# The longest heading and message the shell's send route takes.
_PUSH_TITLE_MAX_LENGTH = 200
_PUSH_MESSAGE_MAX_LENGTH = 4000
_CHAT_TITLE_MAX_LENGTH = 200
_UNKNOWN_CHAT_TITLE = "A chat"

# This module sits five directories below the repo root
# (.agents/skills/notify-user/python/notify_user_skill/).
_REPO_ROOT = Path(__file__).resolve().parents[5]


class HttpClient:
    """Indirection over the outbound HTTP so tests can intercept it."""

    def post_json(
        self, url: str, payload: dict, headers: dict, timeout: float
    ) -> tuple[int | None, str]:
        """POST a JSON body; return ``(status, response text)``, status ``None`` if unreachable, too slow, or
        garbled."""
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


class ChatWatch:
    """What the chat app says about who is watching this chat and whose it is."""

    def __init__(
        self,
        watched_by: list[str],
        owner_watched_by: list[str],
        is_owners_chat: bool,
        chat_title: str | None,
    ) -> None:
        self.watched_by = watched_by
        self.owner_watched_by = owner_watched_by
        self.is_owners_chat = is_owners_chat
        self.chat_title = chat_title


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _chat_id(environ: dict[str, str]) -> str:
    """This chat's id ("" when unknown): the chat's own, else the agent's, which a chat that never moved shares."""
    return environ.get(ENV_CHAT_ID, "") or environ.get(ENV_AGENT_ID, "")


def _notification_body(message: str, title: str | None) -> dict[str, str]:
    """The notification as both the chat app and the Imbue Studio app take it."""
    body = {"message": message.strip()}
    if title:
        body["title"] = title.strip()
    return body


def show_on_desktop(
    message: str, title: str | None, *, http: HttpClient, environ: dict[str, str]
) -> None:
    """Tell the chat app about the notification, so the workspace's desktop shows it over the agent; a warning on
    stderr when it cannot, since the notification still goes to the app."""
    chat_id = _chat_id(environ)
    if not chat_id:
        reason = f"{ENV_CHAT_ID} is not set"
    else:
        status, text = http.post_json(
            f"{message_chat.chat_app_url(environ, _REPO_ROOT)}/api/chats/{chat_id}/notification",
            _notification_body(message, title),
            {"Content-Type": "application/json"},
            timeout=_CHAT_APP_TIMEOUT_SECONDS,
        )
        if status == 200:
            return
        reason = (
            f"the chat app did not answer: {text}"
            if status is None
            else f"the chat app answered {status}"
        )
    sys.stderr.write(
        f"notify-user: could not show the notification on the workspace's desktop ({reason})\n"
    )


def watchers_of_chat(http: HttpClient, environ: dict[str, str]) -> ChatWatch:
    """Who is watching this chat and whether it is the owner's own; raises WatchersUnknownError naming why that is
    unknown."""
    chat_id = _chat_id(environ)
    if not chat_id:
        raise WatchersUnknownError(f"{ENV_CHAT_ID} is not set")
    status, text = http.get(
        f"{message_chat.chat_app_url(environ, _REPO_ROOT)}/api/chats/{chat_id}/watchers",
        timeout=_CHAT_APP_TIMEOUT_SECONDS,
    )
    if status is None:
        raise WatchersUnknownError(f"the chat app did not answer: {text}")
    if status != 200:
        raise WatchersUnknownError(f"the chat app answered {status}")
    try:
        answer = json.loads(text)
    except ValueError as exc:
        raise WatchersUnknownError("the chat app's answer is not JSON") from exc
    if not isinstance(answer, dict):
        raise WatchersUnknownError("the chat app's answer is not a JSON object")
    watched_by = answer.get("watched_by")
    owner_watched_by = answer.get("owner_watched_by")
    is_owners_chat = answer.get("is_owners_chat")
    chat_title = answer.get("chat_title")
    if not _is_string_list(watched_by):
        raise WatchersUnknownError("the chat app's answer carries no list of watchers")
    if not _is_string_list(owner_watched_by) or not isinstance(is_owners_chat, bool):
        raise WatchersUnknownError(
            "the chat app's answer does not say whose chat this is"
        )
    return ChatWatch(
        watched_by=watched_by,
        owner_watched_by=owner_watched_by,
        is_owners_chat=is_owners_chat,
        chat_title=chat_title if isinstance(chat_title, str) else None,
    )


def _watch_or_none(http: HttpClient, environ: dict[str, str]) -> ChatWatch | None:
    """Who is watching this chat and whose it is, or None (warned on stderr) when that cannot be told."""
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
    show_on_desktop(message, title, http=http, environ=environ)
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
    payload: dict[str, str | list[str]] = {**_notification_body(message, title)}
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
    ``?``, so the shell can read it."""
    return text.encode("utf-8", "replace").decode("utf-8")


def push_request(
    message: str, title: str | None, watch: ChatWatch, chat_id: str
) -> dict[str, str]:
    """What the shell's send route takes for this notification: the chat, its title, the agent's words, each cut to
    the route's bounds, and the chat's page in the chat app, which a tap opens. The shell decides what the device
    shows."""
    request = {
        "chat_id": chat_id,
        "app": message_chat.CHAT_APP_NAME,
        "app_path": "/?" + urllib.parse.urlencode({"chat": chat_id}),
        "chat_title": _cut(
            _utf8_text((watch.chat_title or "").strip()) or _UNKNOWN_CHAT_TITLE,
            _CHAT_TITLE_MAX_LENGTH,
        ),
        "message": _cut(_utf8_text(message), _PUSH_MESSAGE_MAX_LENGTH),
    }
    stripped_title = _utf8_text((title or "").strip())
    if stripped_title:
        request["title"] = _cut(stripped_title, _PUSH_TITLE_MAX_LENGTH)
    return request


def _send_token(environ: dict[str, str]) -> str | None:
    """The shell's send token, or None when it cannot be read."""
    path = Path(
        environ.get(ENV_SEND_TOKEN_FILE) or (_REPO_ROOT / DEFAULT_SEND_TOKEN_PATH)
    )
    try:
        return path.read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def push_to_devices(
    message: str,
    title: str | None,
    watch: ChatWatch | None,
    *,
    http: HttpClient,
    environ: dict[str, str],
) -> bool:
    """Ask the shell to push the notification to the owner's devices; return whether it queued it. Only from the
    owner's own chat, and not while one of the owner's pages is watching it. Why nothing was pushed is said on
    stderr, except for a workspace with no registered device."""
    chat_id = _chat_id(environ)
    if watch is None or not chat_id:
        sys.stderr.write(
            "notify-user: not pushed to your devices: whose chat this is could not be told.\n"
        )
        return False
    if not watch.is_owners_chat:
        sys.stderr.write(
            "notify-user: not pushed to your devices: someone other than the owner wrote in this chat.\n"
        )
        return False
    if watch.owner_watched_by:
        return False
    token = _send_token(environ)
    if token is None:
        sys.stderr.write(
            "notify-user: could not push the notification to your devices (the workspace's send token is unreadable).\n"
        )
        return False
    shell_url = (environ.get(ENV_SHELL_URL) or DEFAULT_SHELL_URL).rstrip("/")
    status, text = http.post_json(
        f"{shell_url}{WEB_PUSH_SEND_PATH}",
        push_request(message, title, watch, chat_id),
        {"Content-Type": "application/json", SEND_TOKEN_HEADER: token},
        timeout=_PUSH_TIMEOUT_SECONDS,
    )
    if status == 202:
        return True
    if status == 200:
        return False
    try:
        detail = str(json.loads(text).get("detail", ""))
    except (ValueError, AttributeError):
        detail = text.strip()[:200]
    if status == 429:
        sys.stderr.write(f"notify-user: not pushed to your devices: {detail}\n")
        return False
    reason = (
        f"was unreachable: {text}" if status is None else f"answered {status}: {detail}"
    )
    sys.stderr.write(
        f"notify-user: could not push the notification to your devices (the workspace {reason}).\n"
    )
    return False


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
    watch = _watch_or_none(http, environ)
    is_app_accepted = post_to_studio_app(
        stripped,
        title,
        None if watch is None else watch.watched_by,
        http=http,
        environ=environ,
    )
    is_pushed = push_to_devices(stripped, title, watch, http=http, environ=environ)
    if is_pushed:
        sys.stderr.write(
            "notify-user: also queued for your devices.\n"
            if is_app_accepted
            else "notify-user: queued for your devices, though not taken by the Imbue Studio app.\n"
        )
    is_sent = is_app_accepted or is_pushed
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
