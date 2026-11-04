"""The chat app over HTTP, and ``mngr create`` for a chat that does not exist yet."""

import json
import os
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import httpx
from loguru import logger
from pydantic import Field

from routines.errors import ChatAppError
from routines.errors import ChatCreateError
from routines.interfaces import ChatAppInterface
from routines.interfaces import ClockInterface
from routines.primitives import ChatStatusWord
from routines.primitives import RoutineId
from routines.primitives import SendVerdict
from routines.registry import chat_app_url

# The create template every routine's chat is made from: the one the automation runner used.
AUTOMATION_TEMPLATE: Final[str] = "automation"
CONNECT_TIMEOUT_SECONDS: Final[float] = 3.0
READ_TIMEOUT_SECONDS: Final[float] = 30.0
# A send answers only once the harness has accepted the text, which can take a while on a cold agent.
SEND_READ_TIMEOUT_SECONDS: Final[float] = 300.0
# A 503 is the chat app up but not ready; it is retried for this long before it counts as a refusal.
NOT_READY_RETRY_WINDOW_SECONDS: Final[float] = 30.0
NOT_READY_RETRY_PAUSE_SECONDS: Final[float] = 1.0
CREATE_TIMEOUT_SECONDS: Final[float] = 300.0
HELD_SEND_STATUS: Final[int] = 202
LIST_TIMEOUT_SECONDS: Final[float] = 60.0
# The label a routine's chat carries, and the one the retired automation runner gave its agent.
ROUTINE_LABEL: Final[str] = "routine"
LEGACY_AUTOMATION_LABEL: Final[str] = "automation"
_EVENTS_PAGE_LIMIT: Final[int] = 200
_ASSISTANT_MESSAGE_TYPE: Final[str] = "assistant_message"


def workspace_label(environ: dict[str, str]) -> str:
    """The workspace the services agent belongs to, so the routine's chat groups with the user's other chats."""
    host_dir = environ.get("MNGR_HOST_DIR", "")
    agent_id = environ.get("MNGR_AGENT_ID", "")
    if host_dir and agent_id:
        agent_record = Path(host_dir) / "agents" / agent_id / "data.json"
        try:
            labels = json.loads(agent_record.read_text()).get("labels", {})
        except (OSError, ValueError) as e:
            logger.debug("No workspace label from {}: {}", agent_record, e)
            labels = {}
        if isinstance(labels, dict) and isinstance(labels.get("workspace"), str) and labels["workspace"]:
            return labels["workspace"]
    if host_dir:
        host_record = Path(host_dir) / "data.json"
        try:
            host_name = json.loads(host_record.read_text()).get("host_name", "")
        except (OSError, ValueError) as e:
            logger.debug("No host name from {}: {}", host_record, e)
            host_name = ""
        if isinstance(host_name, str):
            return host_name
    return ""


def _newest_assistant_text(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == _ASSISTANT_MESSAGE_TYPE and isinstance(event.get("text"), str):
            return event["text"]
    return None


def _created_agent_id(stdout: str) -> str:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            logger.trace("Skipping a line of the create's output that is not JSON: {}", line)
            continue
        if isinstance(event, dict) and event.get("event") == "created" and isinstance(event.get("agent_id"), str):
            return event["agent_id"]
    return ""


class HttpChatApp(ChatAppInterface):
    """The chat app reached on loopback, found through the app registry."""

    repo_root: Path = Field(frozen=True, description="The workspace checkout, which a create runs from")
    registry_path: Path = Field(frozen=True, description="The app registry, where the chat app's address is")
    environ: dict[str, str] = Field(frozen=True, description="The process environment")
    clock: ClockInterface = Field(frozen=True, description="For the pauses between not-ready retries")

    def _base_url(self) -> str:
        return chat_app_url(self.registry_path)

    def _request(self, method: str, path: str, body: dict[str, Any] | None, read_timeout: float) -> httpx.Response:
        url = f"{self._base_url()}{path}"
        timeout = httpx.Timeout(connect=CONNECT_TIMEOUT_SECONDS, read=read_timeout, write=10.0, pool=10.0)
        try:
            return httpx.request(method, url, json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise ChatAppError(f"The chat app could not be reached ({e}).") from e

    def _request_until_ready(
        self, method: str, path: str, body: dict[str, Any] | None, read_timeout: float
    ) -> httpx.Response:
        """Request, retrying a 503 (the chat app up but not ready) for a bounded window."""
        deadline = self.clock.now().timestamp() + NOT_READY_RETRY_WINDOW_SECONDS
        response = self._request(method, path, body, read_timeout)
        while response.status_code == 503 and self.clock.now().timestamp() < deadline:
            self.clock.pause(NOT_READY_RETRY_PAUSE_SECONDS)
            response = self._request(method, path, body, read_timeout)
        return response

    def _detail(self, response: httpx.Response) -> str:
        try:
            parsed = response.json()
        except ValueError:
            return response.text.strip()[:200]
        if isinstance(parsed, dict):
            for key in ("detail", "error", "message"):
                if isinstance(parsed.get(key), str):
                    return parsed[key]
        return response.text.strip()[:200]

    def chat_status(self, chat_id: str) -> ChatStatusWord | None:
        response = self._request_until_ready("GET", "/api/chats", None, READ_TIMEOUT_SECONDS)
        if response.status_code != 200:
            raise ChatAppError(f"The chat app answered {response.status_code} for the chat list.")
        try:
            chats = response.json().get("chats", [])
        except (ValueError, AttributeError) as e:
            raise ChatAppError("The chat app's chat list is not JSON.") from e
        for chat in chats:
            if isinstance(chat, dict) and chat.get("chat_id") == chat_id:
                status = str(chat.get("status", "")).upper()
                return ChatStatusWord(status) if status in ChatStatusWord.__members__ else ChatStatusWord.UNKNOWN
        return None

    def _post_verb(self, chat_id: str, verb: str) -> None:
        response = self._request_until_ready(
            "POST", f"/api/chats/{urllib.parse.quote(chat_id)}/{verb}", {}, READ_TIMEOUT_SECONDS
        )
        if response.status_code >= 300:
            raise ChatAppError(f"The chat app refused to {verb} the chat: {self._detail(response)}")

    def stop_chat(self, chat_id: str) -> None:
        self._post_verb(chat_id, "stop")

    def start_chat(self, chat_id: str) -> None:
        self._post_verb(chat_id, "start")

    def interrupt_chat(self, chat_id: str) -> None:
        self._post_verb(chat_id, "interrupt")

    def send_message(self, chat_id: str, text: str) -> SendVerdict:
        body = {"message": text, "message_id": uuid4().hex}
        response = self._request_until_ready(
            "POST",
            f"/api/chats/{urllib.parse.quote(chat_id)}/message",
            body,
            SEND_READ_TIMEOUT_SECONDS,
        )
        if response.status_code >= 300:
            raise ChatAppError(f"The chat app refused the message: {self._detail(response)}")
        # A 202 is the chat app holding the message while the chat moves to a new agent, which gets it next.
        return SendVerdict.HELD if response.status_code == HELD_SEND_STATUS else SendVerdict.ACCEPTED

    def _events(self, chat_id: str, query: str) -> list[dict[str, Any]]:
        response = self._request_until_ready(
            "GET",
            f"/api/chats/{urllib.parse.quote(chat_id)}/events?{query}",
            None,
            READ_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise ChatAppError(f"The chat app answered {response.status_code} for the chat's transcript.")
        try:
            events = response.json().get("events", [])
        except (ValueError, AttributeError) as e:
            raise ChatAppError("The chat app's transcript is not JSON.") from e
        return [event for event in events if isinstance(event, dict)]

    def latest_event_id(self, chat_id: str) -> str | None:
        events = self._events(chat_id, "limit=1")
        if not events:
            return None
        event_id = events[-1].get("event_id")
        return event_id if isinstance(event_id, str) else None

    def assistant_text_after(self, chat_id: str, cursor_event_id: str | None) -> str | None:
        # The transcript answers an empty page for a cursor it cannot place, never a refusal: a cleared chat
        # keeps its earlier events readable, so the cursor taken before the send stays good. A page after a
        # cursor holds the events that immediately follow it, so a long turn is read page by page to its end;
        # without a cursor one page is the newest events, which is all there is to read.
        if cursor_event_id is None:
            return _newest_assistant_text(self._events(chat_id, f"limit={_EVENTS_PAGE_LIMIT}"))
        newest: str | None = None
        cursor: str | None = cursor_event_id
        while cursor is not None:
            events = self._events(chat_id, f"after={urllib.parse.quote(cursor)}&limit={_EVENTS_PAGE_LIMIT}")
            text = _newest_assistant_text(events)
            if text is not None:
                newest = text
            last_event_id = events[-1].get("event_id") if events else None
            is_last_page = (
                len(events) < _EVENTS_PAGE_LIMIT or not isinstance(last_event_id, str) or last_event_id == cursor
            )
            cursor = None if is_last_page else last_event_id
        return newest

    def _run_mngr(self, argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        """Run ``uv run --no-sync mngr <argv>`` from the repo root; raises OSError or TimeoutExpired as they come."""
        return subprocess.run(
            ["uv", "run", "--no-sync", "mngr", *argv],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={**os.environ, **self.environ},
        )

    def find_chat(self, routine_id: RoutineId) -> str | None:
        """An agent labelled for the routine, or the retired runner's for the same skill, so a chat that outlived
        its state (a migrated automation, a routine removed and added again) is taken up rather than created
        under a name mngr would refuse as taken."""
        condition = f'labels.{ROUTINE_LABEL} == "{routine_id}" || labels.{LEGACY_AUTOMATION_LABEL} == "{routine_id}"'
        try:
            completed = self._run_mngr(
                ["list", "--include", condition, "--ids", "--on-error", "continue"], LIST_TIMEOUT_SECONDS
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("Could not look for an agent of the routine {}: {}", routine_id, e)
            return None
        if completed.returncode != 0:
            logger.warning(
                "Could not look for an agent of the routine {}: mngr list exited {}", routine_id, completed.returncode
            )
            return None
        ids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return ids[0] if ids else None

    def create_chat(self, name: str, routine_id: RoutineId, first_message: str) -> str:
        argv = [
            "create",
            str(routine_id),
            "--template",
            AUTOMATION_TEMPLATE,
            "--no-connect",
            "--format",
            "jsonl",
            "--label",
            f"{ROUTINE_LABEL}={routine_id}",
            "--label",
            f"display_name={name}",
        ]
        workspace = workspace_label(self.environ)
        if workspace:
            argv += ["--label", f"workspace={workspace}"]
        argv += ["--message", first_message]
        try:
            completed = self._run_mngr(argv, CREATE_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ChatCreateError(f"The chat could not be created ({e}).") from e
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            reason = detail[-1] if detail else f"mngr create exited {completed.returncode}"
            raise ChatCreateError(f"The chat could not be created: {reason}")
        chat_id = _created_agent_id(completed.stdout)
        if not chat_id:
            raise ChatCreateError("The chat was created but mngr named no agent in its output.")
        logger.debug("Created the chat {} for the routine {}", chat_id, routine_id)
        return chat_id
