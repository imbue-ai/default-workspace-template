"""Starts the cleanup helper: a chat that walks the user through freeing disk space.

The chat is made through the workspace's chat app (``message_chat.py --create``), labelled to open
on the user's screen, and its first message runs the ``free-disk-space`` skill, which sends the
user a notification, opens this app beside the chat, and starts the conversation. While a helper
chat from earlier is still there, a new request goes to it instead of making a second one.
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Final

from app_manifest.primitives import AppName
from loguru import logger
from workspace_layout.client import ShellLayoutClient
from workspace_layout.errors import ShellOpError
from workspace_layout.ops import OpRequester, ShowArgs
from workspace_layout.primitives import WindowPage, WindowPath
from workspace_layout.shell_url import shell_base_url

from disk_space import store

REPO_ROOT: Final[Path] = Path("/home/user/workspace")
# Relative to the repo root, which the app runs from.
# own-process: other-tier -- the chat app's client is the workspace's bare-tier message_chat.py, which only runs as
# ``python3 <path>`` under with_agent_env.sh; importing it would tie this app to an interpreter it does not run on,
# and its exit codes (delivered, blocked, gone) are what the helper acts on.
MESSAGE_CHAT: Final[str] = "system/scripts/message_chat.py"
WITH_AGENT_ENV: Final[str] = "system/libs/routines/with_agent_env.sh"
CHAT_TITLE: Final[str] = "Free up disk space"
CREATE_TIMEOUT_SECONDS: Final[float] = 180.0
# message_chat's codes: the text was handed over, the text landed behind a dialog, and no such chat any more.
CHAT_DELIVERED: Final[int] = 0
CHAT_DELIVERED_BUT_BLOCKED: Final[int] = 7
CHAT_GONE: Final[int] = 8
# The workspace's chat app, whose page the helper's chat lives on.
CHAT_APP: Final[AppName] = AppName("chat")
# The app itself asks the shell for a window; no chat of the user's is the requester.
THIS_APP_REQUESTER: Final[OpRequester] = OpRequester(
    app=AppName("disk-space"), marker=""
)
# One loopback request the shell answers without work; past this it is not answering.
SHELL_TIMEOUT_SECONDS: Final[float] = 2.0


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Runs a workspace command, with its own agent environment; a command that never answers is a failure, not a
    crash: its stderr says so and the caller reports it to the page."""
    wrapper = (
        [str(REPO_ROOT / WITH_AGENT_ENV)]
        if (REPO_ROOT / WITH_AGENT_ENV).exists()
        else []
    )
    try:
        return subprocess.run(
            [*wrapper, *argv],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=CREATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            f"The chat app did not answer within {int(CREATE_TIMEOUT_SECONDS)} seconds.",
        )
    except OSError as error:
        return subprocess.CompletedProcess(
            argv, 1, "", f"The chat app could not be run: {error.strerror or error}"
        )


def _first_message(reason: str, item: str | None) -> str:
    lines = ["/free-disk-space", "", reason]
    if item:
        lines += ["", f"Start with this item the user asked about: {item}"]
    return "\n".join(lines)


def _shell() -> ShellLayoutClient:
    """The shell's layout client: how this app asks for a window or says what it has."""
    return ShellLayoutClient(
        shell_url=shell_base_url(),
        requester=THIS_APP_REQUESTER,
        timeout_seconds=SHELL_TIMEOUT_SECONDS,
    )


def _show_chat(chat_id: str) -> None:
    """Puts the helper's chat on screen: the shell raises a window already there, else points one at it."""
    try:
        _shell().show(
            ShowArgs(
                app=CHAT_APP,
                path=WindowPath(f"/?chat={chat_id}"),
                showing=(WindowPath(f"/{chat_id}"),),
                repoint=(WindowPage("/"),),
            )
        )
    except ShellOpError as error:
        # The chat exists either way; the user can still reach it from the chat list.
        logger.warning("Could not put the cleanup helper's chat on screen: {}", error)


def start(reason: str, trigger: str, item: str | None = None) -> dict[str, Any]:
    """Starts the helper chat, or hands the request to the one already there; returns its chat id and how."""
    watch = store.read_watch()
    existing = watch.get("helper_chat_id")
    if isinstance(existing, str) and existing:
        sent = _run(
            ["python3", MESSAGE_CHAT, existing, "-m", _first_message(reason, item)]
        )
        if sent.returncode in (CHAT_DELIVERED, CHAT_DELIVERED_BUT_BLOCKED):
            if trigger != "check":
                _show_chat(existing)
            store.append_history(
                "helper",
                f"Asked the cleanup helper again ({trigger}).",
                chat_id=existing,
                trigger=trigger,
                item=item,
                reason=reason,
            )
            return {"chat_id": existing, "created": False, "detail": ""}
        # CHAT_GONE (or a refusal) falls through: a chat the app no longer takes cannot be messaged, so the helper
        # gets a new one below.
    for suffix in ("", " 2", " 3", " 4", " 5"):
        created = _run(
            [
                "python3",
                MESSAGE_CHAT,
                "--create",
                "--name",
                CHAT_TITLE + suffix,
                "--label",
                "auto_open=true",
                "--label",
                "app=disk-space",
                "-m",
                _first_message(reason, item),
            ]
        )
        if (
            created.returncode == CHAT_DELIVERED
            or "taken" not in created.stderr.lower()
        ):
            break
    if created.returncode != CHAT_DELIVERED:
        detail = (
            created.stderr.strip().splitlines() or ["The chat app did not answer."]
        )[-1]
        store.append_history(
            "helper-failed",
            "Could not start the cleanup helper.",
            detail=detail,
            trigger=trigger,
        )
        return {"chat_id": "", "created": False, "detail": detail}
    chat_id = ""
    for line in created.stdout.splitlines():
        try:
            chat_id = str(json.loads(line).get("chat_id") or "")
        except (ValueError, AttributeError):
            continue
    if not chat_id:
        detail = "The chat app made a chat but did not say which one."
        store.append_history(
            "helper-failed",
            "Could not start the cleanup helper.",
            detail=detail,
            trigger=trigger,
        )
        return {"chat_id": "", "created": False, "detail": detail}
    store.update_watch(helper_chat_id=chat_id, helper_started_at=time.time())
    store.append_history(
        "helper",
        f"Started the cleanup helper ({trigger}).",
        chat_id=chat_id,
        trigger=trigger,
        item=item,
        reason=reason,
    )
    return {"chat_id": chat_id, "created": True, "detail": ""}
