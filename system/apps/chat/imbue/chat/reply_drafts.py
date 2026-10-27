"""Stream the reply an agent is writing to the pages showing its chat, before its transcript has it.

Claude records a reply only once it has finished writing it, so the chat would otherwise show a
reply seconds after the agent's terminal does. One thread asks each watcher whose chat the user
is looking at for its draft (``AgentSessionWatcher.read_reply_draft``) a few times a second and sends
any change on the chat's stream as an ``assistant_draft`` frame: ``{"type": "assistant_draft",
"agent_id", "after_event_id", "text"}``, with ``text`` empty once there is no draft. The frame is
not a transcript event (it has no ``event_id`` and nothing stores it); the page shows it at the
end of the transcript until the reply itself lands.

The work is bounded by what the user is looking at: only a chat a page streams *and* reports
shown and focused (its presence, ``presence.py``) is read, so background chats, chats in a hidden
or unfocused window, and chats the chat root keeps loaded out of sight cost nothing; and a
looked-at Claude agent with no turn in flight costs one stat of its ``active`` marker per pass.
"""

import threading
from collections.abc import Callable
from typing import Any
from typing import Final
from typing import Protocol

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.harnesses.session_watcher import CapturePaneCallback
from imbue.chat.harnesses.session_watcher import ReplyDraft
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.mutable_model import MutableModel

REPLY_DRAFT_EVENT_TYPE: Final[str] = "assistant_draft"

# How often a looked-at chat's draft is re-read. Each read of a turn in flight is one tmux capture.
REPLY_DRAFT_POLL_INTERVAL_SECONDS: Final[float] = 0.3

_CAPTURE_TIMEOUT_SECONDS: Final[float] = 5.0


class ReplyDraftReader(Protocol):
    """The slice of a session watcher the publisher reads; every ``AgentSessionWatcher`` has it."""

    def read_reply_draft(self, capture_pane: CapturePaneCallback) -> ReplyDraft | None: ...


# ``(chat id, agent id, watcher)`` for each agent with a watcher whose chat the user is looking at.
ListWatchedAgents = Callable[[], list[tuple[str, str, ReplyDraftReader]]]
# Captures one agent's pane by agent id.
CaptureAgentPane = Callable[[str], str | None]
# Sends one frame on a chat's streams.
BroadcastToChat = Callable[[str, dict[str, Any]], None]


def capture_tmux_session_pane(session_name: str) -> str | None:
    """The visible pane of a tmux session with its colour codes and wrapped lines joined, or None."""
    try:
        result = run_local_command_modern_version(
            command=["tmux", "capture-pane", "-e", "-J", "-p", "-t", f"={session_name}:"],
            cwd=None,
            is_checked=False,
            timeout=_CAPTURE_TIMEOUT_SECONDS,
        )
    except ProcessError as e:
        logger.debug("Could not capture tmux session {}: {}", session_name, e)
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def reply_draft_frame(agent_id: str, draft: ReplyDraft) -> dict[str, Any]:
    return {
        "type": REPLY_DRAFT_EVENT_TYPE,
        "agent_id": agent_id,
        "after_event_id": draft.after_event_id,
        "text": draft.text,
    }


class ReplyDraftPublisher(MutableModel):
    """Reads the drafts of the chats the user is looking at on one thread and broadcasts what changed."""

    model_config = {"arbitrary_types_allowed": True}

    list_watched_agents: ListWatchedAgents = Field(
        frozen=True, description="The watchers of the agents whose chats the user is looking at"
    )
    capture_agent_pane: CaptureAgentPane = Field(frozen=True, description="Captures one agent's pane by agent id")
    broadcast_to_chat: BroadcastToChat = Field(frozen=True, description="Sends one frame on a chat's streams")

    # The last draft sent per agent, and the chat it went to, so an unchanged draft sends nothing.
    _published_by_agent: dict[str, tuple[str, ReplyDraft]] = PrivateAttr(default_factory=dict)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        """Begin polling on a daemon thread. Idempotent."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="reply-drafts")
        self._thread.start()

    def stop(self) -> None:
        """Stop polling. Idempotent."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=_CAPTURE_TIMEOUT_SECONDS + 1.0)

    def publish_once(self) -> None:
        """One pass: read each looked-at agent's draft and broadcast the ones that changed."""
        seen_agent_ids: set[str] = set()
        for chat_id, agent_id, watcher in self.list_watched_agents():
            seen_agent_ids.add(agent_id)
            draft = watcher.read_reply_draft(self._bind_capture(agent_id))
            if draft is None:
                continue
            published = self._published_by_agent.get(agent_id)
            previous = None if published is None else published[1]
            is_already_clear = draft.text == "" and (previous is None or previous.text == "")
            if previous == draft or is_already_clear:
                continue
            self._published_by_agent[agent_id] = (chat_id, draft)
            self.broadcast_to_chat(chat_id, reply_draft_frame(agent_id, draft))
        # A chat the user looked away from loses its draft, which nothing would update or clear
        # while they are away, and starts from nothing when they look at it again.
        for agent_id in set(self._published_by_agent) - seen_agent_ids:
            chat_id, draft = self._published_by_agent.pop(agent_id)
            if draft.text != "":
                self.broadcast_to_chat(chat_id, reply_draft_frame(agent_id, ReplyDraft(after_event_id="", text="")))

    def _bind_capture(self, agent_id: str) -> CapturePaneCallback:
        return lambda: self.capture_agent_pane(agent_id)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=REPLY_DRAFT_POLL_INTERVAL_SECONDS):
            try:
                self.publish_once()
            except OSError as e:
                logger.opt(exception=e).warning("Reading the looked-at chats' reply drafts failed")
