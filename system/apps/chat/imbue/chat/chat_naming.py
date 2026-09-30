"""Naming a new chat from its first message: "Short name: brief description", set once.

A chat is minted as "Chat N". Its first message is sent to a small model on the account the
chat runs on (the harness's ``OneShotCompletion``), outside the chat's transcript, and the
answer replaces the minted name through the ordinary rename. The name is set once and never
follows the chat as its topic drifts, so it stays where the user learned it. A chat whose
opening message has no clear subject ("hi") keeps its minted name and is tried again on its
next message, up to ``MAX_NAMING_ATTEMPTS`` messages. A seeded chat (the Mind app's
"Welcome") is named the same way from the first message the user sends in it; a chat named any
other way (by the user, by an agent) is never touched.

What has been tried is kept in the chat's own folder, so a restart of the app neither renames a
named chat nor retries past the limit.
"""

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.models import AgentRenameError
from imbue.chat.models import ChatAccountBinding
from imbue.chat.models import ChatConvergingError
from imbue.chat.naming import canonical_agent_name
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

CHAT_NAMING_FILENAME: Final[str] = "naming.json"
MAX_NAMING_ATTEMPTS: Final[int] = 3

# The model's answer for a message with nothing to name yet.
_NO_NAME_ANSWER: Final[str] = "NONE"
_MAX_NAME_LENGTH: Final[int] = 60
# Well under the pipe buffer the prompt is written to stdin through (16KiB on macOS) at four
# bytes a character; the opening of a long message says what it is about.
_MAX_PROMPT_CHARACTERS: Final[int] = 3000
# How long a chat created with its first message may take to come up before naming gives up on it.
_AGENT_WAIT_SECONDS: Final[float] = 600.0
_AGENT_POLL_SECONDS: Final[float] = 2.0
_NAMING_THREADS: Final[int] = 2

CHAT_NAMING_SYSTEM_PROMPT: Final[str] = (
    "You name chats for a sidebar list. You are given the first message a person sent in a new chat. "
    'Reply with one line in the form "Short name: brief description" and nothing else. '
    "The short name is one to three words naming the subject. "
    "The description is three to seven words saying what the person wants, starting with a lowercase letter "
    "unless it begins with a proper noun. "
    f"Keep the whole line under {_MAX_NAME_LENGTH} characters. No quotes, no emoji, no trailing period. "
    "Write in the language of the message. "
    "If the message has no clear subject yet (a greeting, a test, a single word), "
    f"reply with exactly {_NO_NAME_ANSWER}."
)


class ChatNamingState(FrozenModel):
    """What automatic naming has done for one chat."""

    attempt_count: int = Field(default=0, description="Messages a name was asked for without one being set")
    is_settled: bool = Field(
        default=False, description="The chat has its name (this one or another) and is never named again"
    )


@pure
def parse_generated_chat_name(answer: str) -> str | None:
    """The chat name in a model's answer, or None when it gave none or broke the "Short name: description" form."""
    line = answer.strip().strip("\"'").strip()
    if line == _NO_NAME_ANSWER or "\n" in line:
        return None
    short_name, separator, description = line.partition(": ")
    if not separator or not short_name.strip() or not description.strip():
        return None
    name = f"{short_name.strip()}: {description.strip().rstrip('.')}"
    if len(name) > _MAX_NAME_LENGTH or not canonical_agent_name(name):
        return None
    return name


def read_chat_naming_state(chat_dir: Path) -> ChatNamingState:
    """The chat's naming state as written; a fresh one for a chat with none, or an unreadable file (warned about)."""
    path = chat_dir / CHAT_NAMING_FILENAME
    if not path.is_file():
        return ChatNamingState()
    try:
        return ChatNamingState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as e:
        logger.warning("Ignoring an unreadable chat naming file at {}: {}", path, e)
        return ChatNamingState()


def write_chat_naming_state(chat_dir: Path, state: ChatNamingState) -> None:
    """Write the chat's naming state whole (a temp file renamed into place)."""
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / CHAT_NAMING_FILENAME
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


class ChatNamer(MutableModel):
    """Names chats from their first messages in the background, one attempt per chat at a time."""

    model_config = {"arbitrary_types_allowed": True}

    chat_files_root: Path = Field(frozen=True, description="The folder holding one folder per chat")
    get_active_agent_info: Callable[[ChatId], AgentInfo | None] = Field(
        frozen=True, description="The agent a chat runs on, once it is listed"
    )
    resolve_chat_account_binding: Callable[[ChatId], ChatAccountBinding | None] = Field(
        frozen=True, description="The harness and account a chat runs on, known from the moment its create starts"
    )
    show_automatic_title: Callable[[ChatId, str], None] = Field(
        frozen=True, description="Shows a name for the chat at once, ahead of the rename that makes it its name"
    )
    clear_automatic_title: Callable[[ChatId], None] = Field(
        frozen=True, description="Stops showing a name the rename did not carry"
    )
    has_placeholder_name: Callable[[ChatId], bool] = Field(
        frozen=True, description='Whether a chat still wears a name nobody chose ("Chat N", or its seed title)'
    )
    rename_placeholder_named_chat: Callable[[ChatId, str], bool] = Field(
        frozen=True, description="Renames a chat still wearing a placeholder name; False when it has another"
    )
    build_one_shot_completion: Callable[[HarnessType], OneShotCompletion | None] = Field(
        frozen=True, description="The harness's way to ask one question outside the chat, if it has one"
    )
    agent_wait_seconds: float = Field(default=_AGENT_WAIT_SECONDS, frozen=True, description="See _AGENT_WAIT_SECONDS")
    executor: Executor = Field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=_NAMING_THREADS, thread_name_prefix="chat-naming"),
        frozen=True,
        description="Where naming runs, off the request thread",
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _in_flight_chat_ids: set[ChatId] = PrivateAttr(default_factory=set)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def consider_message(self, chat_id: ChatId, message: str) -> None:
        """Name the chat from this message in the background, unless it is named, out of attempts, or being named."""
        if not message.strip() or self._stop_event.is_set():
            return
        state = read_chat_naming_state(self.chat_files_root / chat_id)
        if state.is_settled or state.attempt_count >= MAX_NAMING_ATTEMPTS:
            return
        with self._lock:
            if chat_id in self._in_flight_chat_ids:
                return
            self._in_flight_chat_ids.add(chat_id)
        self.executor.submit(self._name_chat_then_release, chat_id, message)

    def stop(self) -> None:
        """Stop taking messages and abandon the attempts not yet started."""
        self._stop_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _name_chat_then_release(self, chat_id: ChatId, message: str) -> None:
        try:
            self._name_chat(chat_id, message)
        finally:
            with self._lock:
                self._in_flight_chat_ids.discard(chat_id)

    def _name_chat(self, chat_id: ChatId, message: str) -> None:
        binding = self.resolve_chat_account_binding(chat_id)
        if binding is None:
            logger.debug("Skipped naming chat {}: no account to ask on", chat_id)
            return
        chat_dir = self.chat_files_root / chat_id
        if not self.has_placeholder_name(chat_id):
            write_chat_naming_state(chat_dir, ChatNamingState(is_settled=True))
            return
        completion = self.build_one_shot_completion(binding.harness)
        if completion is None:
            return

        try:
            answer = completion.complete(
                binding.account_dir, CHAT_NAMING_SYSTEM_PROMPT, message[:_MAX_PROMPT_CHARACTERS]
            )
        except OneShotCompletionError as e:
            logger.warning("Failed to generate a name for chat {}: {}", chat_id, e)
            self._record_attempt(chat_dir)
            return
        name = parse_generated_chat_name(answer)
        if name is None:
            logger.debug("Left chat {} unnamed: the answer {!r} gave no name", chat_id, answer)
            self._record_attempt(chat_dir)
            return

        # Show it at once; the rename that makes it the chat's name follows once the chat is listed
        self.show_automatic_title(chat_id, name)
        is_renamed = False
        try:
            if self._wait_for_active_agent(chat_id) is None:
                # A shutdown ends the wait too, and is no reason to spend one of the chat's attempts.
                if not self._stop_event.is_set():
                    logger.debug("Left chat {} unnamed: it did not come up", chat_id)
                    self._record_attempt(chat_dir)
                return
            is_renamed = self.rename_placeholder_named_chat(chat_id, name)
        except (AgentRenameError, ChatConvergingError) as e:
            logger.warning("Failed to name chat {} {!r}: {}", chat_id, name, e)
            self._record_attempt(chat_dir)
            return
        finally:
            if not is_renamed:
                self.clear_automatic_title(chat_id)
        write_chat_naming_state(chat_dir, ChatNamingState(is_settled=True))
        if is_renamed:
            logger.info("Named chat {} {!r}", chat_id, name)

    def _wait_for_active_agent(self, chat_id: ChatId) -> AgentInfo | None:
        deadline = time.monotonic() + self.agent_wait_seconds
        agent_info = self.get_active_agent_info(chat_id)
        while agent_info is None and time.monotonic() < deadline:
            if self._stop_event.wait(_AGENT_POLL_SECONDS):
                return None
            agent_info = self.get_active_agent_info(chat_id)
        return agent_info

    def _record_attempt(self, chat_dir: Path) -> None:
        state = read_chat_naming_state(chat_dir)
        write_chat_naming_state(chat_dir, ChatNamingState(attempt_count=state.attempt_count + 1))
