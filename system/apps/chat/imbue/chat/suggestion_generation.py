"""Working out a chat's next-step suggestions when a turn ends in it.

When an agent finishes a reply, the end of the conversation is sent to a mid-sized model on the
account the chat runs on (the harness's ``OneShotCompletion``), outside the chat's transcript, and
the replies it suggests are stored as the chat's set (``suggestions.py``) for every page of the chat
to read. Every chat is treated alike, whether or not a page is showing it, so a chat's suggestions
are there when its user comes back to it.

A turn's end always settles the stored set: it is replaced when the model suggests something, and
removed when it suggests nothing, when the call fails, and when nothing is asked at all, so a set
never outlives the reply it was made for. A reply is asked about once; a failed call is not retried.
"""

import re
import threading
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import Executor
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.chat_seed import SeedRole
from imbue.chat.chat_seed import SeedTurn
from imbue.chat.chat_seed import tagged_turns
from imbue.chat.chat_settings import ChatSettings
from imbue.chat.dictation_note import without_dictation_notes
from imbue.chat.harnesses.events import DisplayKind
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.one_shot import OneShotCompletion
from imbue.chat.harnesses.one_shot import OneShotCompletionError
from imbue.chat.harnesses.one_shot import OneShotModelTier
from imbue.chat.models import ChatAccountBinding
from imbue.chat.models import ChatSnapshot
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.chat.primitives import ChatStatus
from imbue.chat.suggestions import ChatSuggestions
from imbue.chat.suggestions import Suggestion
from imbue.chat.suggestions import SuggestionSource
from imbue.chat.suggestions import read_chat_suggestions
from imbue.chat.suggestions import remove_chat_suggestions
from imbue.chat.suggestions import write_chat_suggestions
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

# How many turns of the conversation the model is shown, the reply the suggestions follow among them.
_MAX_PROMPT_TURNS: Final[int] = 6
# How much of the conversation the model is shown. The reply itself is never cut to fit.
_MAX_PROMPT_CHARACTERS: Final[int] = 12_000
# What stands where the opening of a turn was cut away.
_CUT_MARK: Final[str] = "[...]\n"
# How far back the transcript is read for those turns: a turn of work is mostly tool calls and their results.
_TAIL_EVENT_COUNT: Final[int] = 300
_MAX_SUGGESTION_COUNT: Final[int] = 10
_MAX_LABEL_LENGTH: Final[int] = 80
# Room to draft, critique and redraft before answering, as the prompt asks.
_THINKING_TOKEN_ALLOWANCE: Final[int] = 4000

# The label an agent carries when another agent started it: a helper nobody talks to.
_AGENT_CREATED_LABEL: Final[str] = "agent_created"
# The user messages that are no part of the conversation's flow: the page draws nothing at their place, so a reply
# they follow is still the chat's last word.
_UNSEEN_USER_MESSAGE_DISPLAYS: Final[frozenset[str]] = frozenset(
    (DisplayKind.HIDDEN, DisplayKind.SKILL_EXPANSION, DisplayKind.STATUS)
)

_SUGGESTION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<suggestion(?P<attributes>\s[^<>]*)?>(?P<label>.*?)</suggestion>", re.DOTALL | re.IGNORECASE
)
_RECOMMENDED_PATTERN: Final[re.Pattern[str]] = re.compile(r"""\brecommended\s*=\s*["']?true\b""", re.IGNORECASE)

# "The assistant" is the chat's agent: the model answering is not that agent, and sees only the turns it is given.
SUGGESTION_GENERATION_SYSTEM_PROMPT: Final[str] = """\
Your task is to come up with reasonable "suggestions": suggested replies that the user might actually want to send as the next turn in the conversation below.

If there is a hard-coded set of replies to be given (for onboarding), simply follow those (see the "Fixed onboarding suggestions" section below, when it is present).

If the suggestions are obvious (for example, the assistant's last response ended with a yes or no question), just give the easy, obvious suggested next steps.

If the assistant's last response requires very complex information from the user (for example, it asked them for their preferences) and suggestions don't make sense, it's ok to emit no suggestions.

If the assistant asked a bunch of complicated questions and it's hard for a suggestion to really make sense, you can suggest "Go with your recommendations" as a blanket "keep going and figure it out".

Otherwise, think carefully about the possible next steps that a user could take from here.
Make a list of 10 possibilities, then critique them. Your critique should include comments on both the actual suggested action, and on the way the suggestion is written. Suggestions should be short, clear, simple, and useful.
Once your critique is done, make a second list where you incorporate those critiques to edit the suggestions and make them better, clearer, simpler, and shorter. It is good for this second pass to filter the list down to just the most reasonable ones: you should generally end up with 2 or 3 suggestions. Then sort them in order from best to worst and output them in the correct format:
    <suggestion>{your text goes here}</suggestion>
If one of the suggestions is the clear best next step, it should be indicated as the "recommended" one:
    <suggestion recommended="true">{your text goes here}</suggestion>
Only one suggestion should ever be recommended (and it is the first one).

Write each suggestion as the user would say it, in the user's voice, in the language the user writes in.
Do all of your drafting and critiquing in your thinking. Your response should ONLY include "<suggestion>...</suggestion>" entries (one per line). Don't reply with anything else."""


class ConversationEnd(FrozenModel):
    """Where a chat's transcript ends, when it ends on a reply: the reply, and the conversation before it."""

    reply_event_id: str = Field(description="The event id of the reply, which a set of suggestions follows")
    reply_text: str = Field(description="What the reply says; empty when it only called tools")
    is_reply_a_failure: bool = Field(description="Whether the reply is a provider's failure rather than the agent's")
    earlier_turns: tuple[SeedTurn, ...] = Field(description="The conversation before the reply, oldest first")


@pure
def _is_turn_of_the_conversation(user_message: Mapping[str, Any]) -> bool:
    """Whether a user message opens a new stretch of the transcript as the page draws it, so a reply before it is
    no longer the chat's last word."""
    return user_message.get("display") not in _UNSEEN_USER_MESSAGE_DISPLAYS


@pure
def _spoken_text(event: Mapping[str, Any]) -> tuple[SeedRole, str] | None:
    """What an event adds to the conversation as the page shows it, or None when it adds nothing: a person's own
    words without the context sent along with them, or the agent's text. Tool calls, thinking, failures, and the
    messages a harness or this app wrote for the agent are no part of it."""
    match event.get("type"):
        case "assistant_message":
            is_failure = event.get("is_api_error") or event.get("is_auth_error")
            said = None if is_failure else event.get("text")
            role = SeedRole.ASSISTANT
        case "user_message":
            display = event.get("display")
            if display is None:
                sent = event.get("content")
            elif display == DisplayKind.PROMPT_WITH_CONTEXT:
                sent = event.get("display_body") or event.get("content")
            else:
                sent = None
            said = without_dictation_notes(sent) if isinstance(sent, str) else None
            role = SeedRole.USER
        case _:
            return None
    if not isinstance(said, str) or not said.strip():
        return None
    return role, said.strip()


@pure
def conversation_turns(events: Sequence[Mapping[str, Any]]) -> tuple[SeedTurn, ...]:
    """The conversation in ``events`` as turns, oldest first: what each side said in a row is one turn."""
    turns: list[SeedTurn] = []
    for event in events:
        spoken = _spoken_text(event)
        if spoken is None:
            continue
        role, text = spoken
        if turns and turns[-1].role is role:
            turns[-1] = SeedTurn(role=role, text=f"{turns[-1].text}\n\n{text}")
        else:
            turns.append(SeedTurn(role=role, text=text))
    return tuple(turns)


@pure
def read_conversation_end(events: Sequence[Mapping[str, Any]]) -> ConversationEnd | None:
    """The reply ``events`` end on, with the conversation before it; None when they end on no reply (they hold
    none, or somebody has spoken since the last one).

    The reply is found the way the page finds the message a set may be drawn under, so the two agree on which
    message a stored set is for.
    """
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        match event.get("type"):
            case "assistant_message":
                event_id = event.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    return None
                text = event.get("text")
                return ConversationEnd(
                    reply_event_id=event_id,
                    reply_text=text.strip() if isinstance(text, str) else "",
                    is_reply_a_failure=bool(event.get("is_api_error") or event.get("is_auth_error")),
                    earlier_turns=conversation_turns(events[:index]),
                )
            case "user_message":
                if _is_turn_of_the_conversation(event):
                    return None
            case _:
                pass
    return None


@pure
def suggestion_generation_prompt(end: ConversationEnd) -> str:
    """What the model is asked about: the reply, whole, under the last turns before it, as tagged turns.

    The earlier turns are cut from the oldest end to fit ``_MAX_PROMPT_CHARACTERS``: the turn the limit falls in
    keeps its end, which is where the conversation was heading.
    """
    room = max(_MAX_PROMPT_CHARACTERS - len(end.reply_text), 0)
    kept: list[SeedTurn] = []
    for turn in reversed(end.earlier_turns[-(_MAX_PROMPT_TURNS - 1) :]):
        if room <= 0:
            break
        if len(turn.text) <= room:
            kept.append(turn)
        else:
            kept.append(SeedTurn(role=turn.role, text=f"{_CUT_MARK}{turn.text[-room:]}"))
        room -= len(turn.text)
    kept.reverse()
    return tagged_turns((*kept, SeedTurn(role=SeedRole.ASSISTANT, text=end.reply_text)))


@pure
def parse_generated_suggestions(answer: str) -> tuple[Suggestion, ...]:
    """The suggestions in a model's answer, in order; none is a valid answer.

    Only the ``<suggestion>`` elements are read, so whatever else the model wrote (a draft, a critique) is no part
    of the set. A label is kept when it is one line of 1 to ``_MAX_LABEL_LENGTH`` characters and no earlier one says
    the same, up to ``_MAX_SUGGESTION_COUNT`` of them; only the first one kept may be the recommended one.
    """
    suggestions: list[Suggestion] = []
    seen_labels: set[str] = set()
    for element in _SUGGESTION_PATTERN.finditer(answer):
        if len(suggestions) == _MAX_SUGGESTION_COUNT:
            break
        label = element.group("label").strip()
        if not label or len(label) > _MAX_LABEL_LENGTH or "\n" in label or label.casefold() in seen_labels:
            continue
        seen_labels.add(label.casefold())
        is_marked_recommended = _RECOMMENDED_PATTERN.search(element.group("attributes") or "") is not None
        suggestions.append(
            Suggestion(
                suggestion_id=f"generated-{len(suggestions) + 1}",
                label=label,
                is_recommended=is_marked_recommended and not suggestions,
            )
        )
    return tuple(suggestions)


@pure
def reason_reply_gets_no_suggestions(end: ConversationEnd) -> str | None:
    """Why no replies are suggested to the reply the chat ends on; None when some may be."""
    if end.is_reply_a_failure:
        return "its last reply is a provider failure"
    if not end.reply_text:
        return "its last reply says nothing"
    return None


@pure
def reason_chat_gets_no_suggestions(snapshot: ChatSnapshot | None, settings: ChatSettings) -> str | None:
    """Why no replies are suggested in the chat as it stands; None when it is an agent chat a person talks to,
    waiting on that person, with next-step suggestions switched on."""
    if not settings.is_next_step_suggestions_enabled:
        return "next-step suggestions are switched off"
    if snapshot is None:
        return "it is not a chat the app lists"
    if snapshot.kind is ChatKind.ROOM:
        return "it is a room"
    if snapshot.labels.get(_AGENT_CREATED_LABEL) == "true":
        return "an agent started it"
    if snapshot.status is not ChatStatus.IDLE or snapshot.handoff is not None:
        return f"it is {snapshot.status}, not waiting on its user"
    agent = snapshot.active_agent
    if agent.queued_messages or agent.is_connecting or agent.is_resending_after_sign_in:
        return "a message is on its way to its agent"
    return None


def _log_generation_failure(chat_id: ChatId, future: Future[None]) -> None:
    """Log what ended a turn's settling that the settling itself did not handle; nothing else reads its future."""
    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        logger.opt(exception=error).error("Working out the suggestions of chat {} failed", chat_id)


class SuggestionGenerator(MutableModel):
    """Works out chats' next-step suggestions in the background as their turns end, one chat's at a time."""

    model_config = {"arbitrary_types_allowed": True}

    chat_files_root: Path = Field(frozen=True, description="The folder holding one folder per chat")
    read_chat_settings: Callable[[], ChatSettings] = Field(
        frozen=True, description="The workspace's chat settings as they stand"
    )
    get_chat_snapshot: Callable[[str], ChatSnapshot | None] = Field(
        frozen=True, description="A chat as the pages see it; None for one the app does not list"
    )
    read_tail_events: Callable[[ChatId, int], list[dict[str, Any]] | None] = Field(
        frozen=True, description="The newest events of a chat's transcript; None for a chat with none to read"
    )
    resolve_chat_account_binding: Callable[[ChatId], ChatAccountBinding | None] = Field(
        frozen=True, description="The harness and account a chat runs on"
    )
    build_one_shot_completion: Callable[[HarnessType], OneShotCompletion | None] = Field(
        frozen=True, description="The harness's way to ask one question outside the chat, if it has one"
    )
    broadcast_suggestions_updated: Callable[[ChatId], None] = Field(
        frozen=True, description="Tells every page that a chat's stored set was replaced or removed"
    )
    executor: Executor = Field(
        frozen=True, description="Where suggestions are worked out, off the thread that saw the turn end"
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _in_flight_chat_ids: set[ChatId] = PrivateAttr(default_factory=set)
    # The chats in which another turn ended while the last one was being settled.
    _chat_ids_to_settle_again: set[ChatId] = PrivateAttr(default_factory=set)
    # The reply each chat's model call in flight is for.
    _pending_reply_event_id_by_chat: dict[ChatId, str] = PrivateAttr(default_factory=dict)
    # The reply the model was last asked about in each chat, so it is never asked about the same one twice.
    _asked_reply_event_id_by_chat: dict[ChatId, str] = PrivateAttr(default_factory=dict)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def consider_turn_end(self, chat_id: ChatId) -> None:
        """A turn ended in the chat: settle its stored set in the background, after the one being settled if any."""
        if self._stop_event.is_set():
            return
        with self._lock:
            if chat_id in self._in_flight_chat_ids:
                self._chat_ids_to_settle_again.add(chat_id)
                return
            self._in_flight_chat_ids.add(chat_id)
        future = self.executor.submit(self._settle_turn_end_then_release, chat_id)
        future.add_done_callback(lambda done: _log_generation_failure(chat_id, done))

    def get_pending_reply_event_id(self, chat_id: ChatId) -> str | None:
        """The reply a set is being worked out for in the chat right now; None when none is."""
        with self._lock:
            return self._pending_reply_event_id_by_chat.get(chat_id)

    def stop(self) -> None:
        """Stop taking turn ends, abandon the settling not yet started, and store nothing a call in flight answers."""
        self._stop_event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _settle_turn_end_then_release(self, chat_id: ChatId) -> None:
        try:
            self._settle_turn_end(chat_id)
        finally:
            with self._lock:
                self._in_flight_chat_ids.discard(chat_id)
                is_another_turn_ended = chat_id in self._chat_ids_to_settle_again
                self._chat_ids_to_settle_again.discard(chat_id)
            if is_another_turn_ended:
                self.consider_turn_end(chat_id)

    def _settle_turn_end(self, chat_id: ChatId) -> None:
        stored = read_chat_suggestions(self.chat_files_root / chat_id)
        if stored is None:
            # Reading a chat's transcript starts following it, so a chat with nothing to ask about and no set to
            # remove is not read: a worker's, a room's, and every chat's while the switch is off.
            reason = reason_chat_gets_no_suggestions(self.get_chat_snapshot(chat_id), self.read_chat_settings())
            if reason is not None:
                logger.debug("Suggested no replies in chat {}: {}", chat_id, reason)
                return
        events = self.read_tail_events(chat_id, _TAIL_EVENT_COUNT)
        if events is None:
            logger.debug("Left the suggestions of chat {} alone: it has no transcript to read", chat_id)
            return
        end = read_conversation_end(events)
        if end is not None and stored is not None and stored.after_event_id == end.reply_event_id:
            return
        ask = None if end is None else self._question_about(chat_id, end)
        if end is None or ask is None:
            self._store(chat_id, None)
            return
        # The mark covers the call alone: a page reads the route on the push the store sends, and the route answers
        # a marked chat as pending ahead of its stored set.
        with self._marked_pending(chat_id, end.reply_event_id):
            try:
                answer = ask()
            except OneShotCompletionError as e:
                logger.warning("Failed to work out suggestions for chat {}: {}", chat_id, e)
                answer = ""
        if self._is_answer_overtaken(chat_id, end.reply_event_id):
            return
        suggestions = parse_generated_suggestions(answer)
        generated = (
            ChatSuggestions(
                after_event_id=end.reply_event_id, source=SuggestionSource.GENERATED, suggestions=suggestions
            )
            if suggestions
            else None
        )
        self._store(chat_id, generated)

    def _question_about(self, chat_id: ChatId, end: ConversationEnd) -> Callable[[], str] | None:
        """The call that asks a model for replies to the chat's last reply; None, with the reason logged, when
        none is to be asked."""
        reason = reason_reply_gets_no_suggestions(end) or reason_chat_gets_no_suggestions(
            self.get_chat_snapshot(chat_id), self.read_chat_settings()
        )
        if reason is not None:
            logger.debug("Suggested no replies in chat {}: {}", chat_id, reason)
            return None
        binding = self.resolve_chat_account_binding(chat_id)
        completion = None if binding is None else self.build_one_shot_completion(binding.harness)
        if binding is None or completion is None:
            logger.debug("Suggested no replies in chat {}: it has no account or harness to ask a model on", chat_id)
            return None
        with self._lock:
            if self._asked_reply_event_id_by_chat.get(chat_id) == end.reply_event_id:
                logger.debug("Suggested no replies in chat {}: its last reply was asked about once already", chat_id)
                return None
            self._asked_reply_event_id_by_chat[chat_id] = end.reply_event_id
        account_dir = binding.account_dir
        prompt = suggestion_generation_prompt(end)
        return lambda: completion.complete(
            account_dir,
            SUGGESTION_GENERATION_SYSTEM_PROMPT,
            prompt,
            model_tier=OneShotModelTier.MEDIUM,
            thinking_token_allowance=_THINKING_TOKEN_ALLOWANCE,
        )

    def _is_answer_overtaken(self, chat_id: ChatId, reply_event_id: str) -> bool:
        """Whether the chat moved on while the model answered, so the answer is for a reply nobody is at any more:
        the app is stopping, the reply is no longer the chat's last word, a turn is in flight, or the switch went
        off."""
        if self._stop_event.is_set():
            return True
        events = self.read_tail_events(chat_id, _TAIL_EVENT_COUNT)
        end = None if events is None else read_conversation_end(events)
        reason = (
            "its last reply changed"
            if end is None or end.reply_event_id != reply_event_id
            else reason_chat_gets_no_suggestions(self.get_chat_snapshot(chat_id), self.read_chat_settings())
        )
        if reason is None:
            return False
        logger.debug("Dropped the suggestions worked out for chat {}: {}", chat_id, reason)
        return True

    def _store(self, chat_id: ChatId, suggestions: ChatSuggestions | None) -> None:
        """Make the chat's stored set ``suggestions``, or none at all, and tell the pages when that changed it."""
        chat_dir = self.chat_files_root / chat_id
        if suggestions is None:
            is_changed = remove_chat_suggestions(chat_dir)
        else:
            write_chat_suggestions(chat_dir, suggestions)
            is_changed = True
        if is_changed:
            self.broadcast_suggestions_updated(chat_id)

    @contextmanager
    def _marked_pending(self, chat_id: ChatId, reply_event_id: str) -> Generator[None, None, None]:
        with self._lock:
            self._pending_reply_event_id_by_chat[chat_id] = reply_event_id
        try:
            yield
        finally:
            with self._lock:
                self._pending_reply_event_id_by_chat.pop(chat_id, None)
