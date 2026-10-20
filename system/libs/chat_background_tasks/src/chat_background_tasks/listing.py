"""Which chats are busy and on what, as the chat app answers it or, failing that, the marker files.

The chat app reads the markers on its state poller and is the authority for everyone else: it
also knows whether a turn is in flight, which the files cannot show. ``GET /api/agents?tracked=true``
answers from the agent list it already follows. When it cannot be reached, or predates the busy
fields, the files answer instead; then agent names are unknown and a chat's active agent is taken
to be the agent whose id is the chat id.
"""

import http.client
import json
import urllib.parse
from pathlib import Path
from typing import Any, NamedTuple

from chat_background_tasks.markers import (
    BackgroundTask,
    json_object,
    live_tasks_by_chat,
)

# The chat app's own agent list, with no fresh discovery; an older app ignores the query and
# answers without the busy fields, which the files then answer for.
AGENTS_PATH = "/api/agents?tracked=true"
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0


class ChatTasks(NamedTuple):
    """One chat's busy verdict and pending tasks."""

    chat_id: str
    active_agent_id: str
    active_agent_name: str
    is_busy: bool
    tasks: tuple[BackgroundTask, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "active_agent_id": self.active_agent_id,
            "active_agent_name": self.active_agent_name,
            "is_busy": self.is_busy,
            "background_tasks": [task.to_json() for task in self.tasks],
        }

    def to_tsv(self) -> str:
        """``chat_id<TAB>active_agent_id<TAB>active_agent_name<TAB>is_busy<TAB>task_count``. Imbue Studio
        reads these columns by position, so a new one may only be appended."""
        fields = (
            self.chat_id,
            self.active_agent_id,
            self.active_agent_name,
            "true" if self.is_busy else "false",
            str(len(self.tasks)),
        )
        return "\t".join(" ".join(field.split()) for field in fields)


class ChatAppUnavailableError(Exception):
    """The chat app could not answer, or answered without the busy fields, so the files decide."""


def get_json(base_url: str, path: str) -> object:
    parsed = urllib.parse.urlsplit(base_url)
    connection = http.client.HTTPConnection(
        parsed.hostname or "127.0.0.1",
        parsed.port or 80,
        timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        connection.connect()
        if connection.sock is not None:
            connection.sock.settimeout(READ_TIMEOUT_SECONDS)
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise ChatAppUnavailableError(
            f"could not reach the chat app at {base_url}: {exc}"
        ) from exc
    finally:
        connection.close()
    if response.status != 200:
        raise ChatAppUnavailableError(
            f"the chat app answered HTTP {response.status} for {path}"
        )
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ChatAppUnavailableError(
            f"the chat app's {path} answer is not JSON"
        ) from exc


def chats_from_agents_payload(payload: object) -> list[ChatTasks]:
    """Every agent's chat in a ``GET /api/agents`` answer, with its busy verdict and pending tasks.

    Raises ``ChatAppUnavailableError`` for an answer without the busy fields (a chat app from
    before them), which the files then answer for.
    """
    fields = json_object(payload)
    agents = None if fields is None else fields.get("agents")
    if not isinstance(agents, list):
        raise ChatAppUnavailableError("the chat app's agent list has no agents")
    chats: list[ChatTasks] = []
    for raw_agent in agents:
        agent = json_object(raw_agent)
        if agent is None or "is_busy" not in agent or "background_tasks" not in agent:
            raise ChatAppUnavailableError(
                "the chat app's agent list predates the busy fields"
            )
        raw_tasks = agent["background_tasks"]
        tasks = tuple(
            task
            for task in (
                BackgroundTask.from_json(raw)
                for raw in (raw_tasks if isinstance(raw_tasks, list) else [])
            )
            if task is not None
        )
        agent_id = str(agent.get("id") or "")
        chats.append(
            ChatTasks(
                chat_id=str(agent.get("chat_id") or agent_id),
                active_agent_id=agent_id,
                active_agent_name=str(agent.get("name") or ""),
                is_busy=bool(agent["is_busy"]),
                tasks=tasks,
            )
        )
    return chats


def chats_from_files(root: Path) -> list[ChatTasks]:
    return [
        ChatTasks(
            chat_id=chat_id,
            active_agent_id=chat_id,
            active_agent_name="",
            is_busy=True,
            tasks=tuple(tasks),
        )
        for chat_id, tasks in live_tasks_by_chat(root).items()
    ]
