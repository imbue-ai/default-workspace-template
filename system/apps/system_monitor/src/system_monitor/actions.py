"""What the Stop buttons do (``docs/system/specs/system-monitor.md`` section 4.6): stop a chat's agent through
the chat app (the reversible ``mngr stop`` its own Stop runs; the next message brings the chat back), stop the
code checkers a chat started (the chat's agent starts them again when it needs them), or quit one of the apps
the user built through supervisord (the shell starts it again when a window opens it).

The workspace's own programs cannot be stopped from here: the breakdown gives them no target.
"""

import os
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import quote

import httpx
from oom_priority.supervisor_rpc import SUPERVISOR_SOCKET, stop_program

from system_monitor import programs, usage

CHAT_PROGRAM: Final[str] = "chat"
# The chat app stops an agent with mngr, which can take a few seconds.
_CHAT_STOP_TIMEOUT_SECONDS: Final[float] = 30.0


class StopRefusedError(Exception):
    """A stop that could not be carried out, with the HTTP status the route answers and why."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def app_url(program: str, registry: Path = programs.APPS_REGISTRY) -> str | None:
    """Where the app the ``program`` runs is served, from the app registry; None when it is not registered."""
    for row in programs.registry_rows(registry):
        if row.get("program") == program and row.get("url"):
            return str(row["url"])
    return None


def stop_chat(chat_id: str, registry: Path = programs.APPS_REGISTRY) -> None:
    """Ask the chat app to stop the chat's agent. Raises StopRefusedError when it cannot or will not."""
    url = app_url(CHAT_PROGRAM, registry)
    if url is None:
        raise StopRefusedError(
            503, "The chat app is not running, so the chat cannot be stopped from here"
        )
    try:
        response = httpx.post(
            f"{url.rstrip('/')}/api/chats/{quote(chat_id, safe='')}/stop",
            timeout=_CHAT_STOP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise StopRefusedError(502, f"Could not reach the chat app: {error}") from error
    if response.status_code >= 400:
        detail = _detail_of(response)
        raise StopRefusedError(
            response.status_code if response.status_code < 500 else 502,
            detail or f"The chat app answered {response.status_code}",
        )


def _detail_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()
    return str(body.get("detail", "")) if isinstance(body, dict) else ""


def stop_checkers(
    agent_id: str,
    procs: Mapping[int, programs.Process] | None = None,
    agents: Mapping[int, str] | None = None,
) -> int:
    """Stop the code checkers a chat's agent started; how many were told to stop.

    Raises StopRefusedError when the chat has none running, or one is not ours to stop.
    """
    live = procs if procs is not None else programs.read_processes()
    registered = agents if agents is not None else programs.agents_by_pid()
    pids = usage.checker_pids_of_chat(live, registered, agent_id)
    if not pids:
        raise StopRefusedError(404, "That chat has no code checkers running")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue  # gone on its own between the walk and now
        except PermissionError as error:
            raise StopRefusedError(403, f"Process {pid} is not ours to stop") from error
    return len(pids)


def stop_app(
    program: str,
    apps: Mapping[str, str] | None = None,
    sock: Path = SUPERVISOR_SOCKET,
) -> None:
    """Quit one of the user's apps through supervisord. Raises StopRefusedError for any other program."""
    user_apps = apps if apps is not None else programs.user_apps()
    if program not in user_apps:
        raise StopRefusedError(403, "Only the apps you built can be quit from here")
    if not stop_program(program, sock):
        raise StopRefusedError(502, f"The workspace did not stop {user_apps[program]}")


def stop(kind: str, target_id: str) -> None:
    """Carry out one Stop button's request. Raises StopRefusedError when it cannot be."""
    match kind:
        case "chat":
            stop_chat(target_id)
        case "checkers":
            stop_checkers(target_id)
        case "app":
            stop_app(target_id)
        case _:
            raise StopRefusedError(400, f"Nothing of kind {kind!r} can be stopped")
