"""Routing quick look-and-feel changes to a separate Sonnet agent.

On every message the user sends to a chat, the chat app asks Jev (TypeSafe's decision model) one
yes/no question about the message and the last few messages of the conversation: is this a quick
look-and-feel change to an app (a colour, font size, spacing, showing or hiding an element,
light/dark styling, restyling a page) that needs no new features, data or backend work?

- Yes: the message is forwarded word for word to the workspace's quick-change agent (one chat per
  workspace, created on first use and reused, running the ``quick-look-and-feel-change`` skill on
  Sonnet), which edits the live app and refreshes the user's window. The chat's own agent still
  receives the message, prefixed with a routing note (``ROUTING_NOTE_TAG``) telling it the change
  is being made elsewhere; the page shows only the user's words. The quick-change agent's report
  reaches the chat as a background-task report through the ordinary send path.
- No, or Jev cannot be reached or times out: the message goes to the chat's agent untouched.

The question is asked before the message is delivered, so the chat's agent never sees an unmarked
quick change; the wait is bounded by ``ROUTING_TIMEOUT_SECONDS``.

Jev is reached with an OpenRouter key saved in the chat app's data folder (``openrouter_api_key``)
when there is one, else through the Imbue LiteLLM proxy the chat's account is signed in to, at its
Jev route (``/jev/v1/systemone``). A chat with neither is never routed.

Every decision (the Jev request, its answer, timings and the outcome) and every forward is
appended to ``quick_change_routing.jsonl`` in the chat app's data folder. The quick-change agent's
chat id is kept in ``quick_change_agent.json`` beside it.
"""

import json
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from loguru import logger as _loguru_logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.errors import ChatAppError
from imbue.chat.harnesses.claude.auth import ANTHROPIC_API_KEY_ENV_VAR
from imbue.chat.harnesses.claude.auth import ANTHROPIC_BASE_URL_ENV_VAR
from imbue.chat.harnesses.claude.auth import read_managed_auth_env
from imbue.chat.harnesses.events import DisplayKind
from imbue.chat.harnesses.message_display import QUICK_CHANGE_ROUTING_NOTE_TAG
from imbue.chat.harnesses.message_display import classify_user_message
from imbue.chat.models import AgentCreationError
from imbue.chat.models import ChatAccountBinding
from imbue.chat.primitives import ChatId
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

logger = _loguru_logger

ROUTING_LOG_FILENAME: Final[str] = "quick_change_routing.jsonl"
QUICK_CHANGE_AGENT_FILENAME: Final[str] = "quick_change_agent.json"
QUICK_CHANGE_SKILL: Final[str] = "quick-look-and-feel-change"
QUICK_CHANGE_CHAT_NAME: Final[str] = "Quick look-and-feel changes"
# The label the quick-change chat carries, so it can be found in `mngr list`.
QUICK_CHANGE_LABEL: Final[str] = "quick_change_agent"
# The model the quick-change chat runs on: the claude harness's Sonnet option id.
QUICK_CHANGE_MODEL_ID: Final[str] = "sonnet[1m]"

JEV_ROUTE_PATH: Final[str] = "/jev/v1/systemone"
# A file in the chat app's data folder holding an OpenRouter key, which sends Jev calls to
# OpenRouter itself rather than through the Imbue proxy.
OPENROUTER_KEY_FILENAME: Final[str] = "openrouter_api_key"
OPENROUTER_JEV_URL: Final[str] = "https://openrouter.ai/api/v1/systemone"
JEV_MODEL: Final[str] = "typesafe/jev-1.13"
# The longest a send waits for Jev before it is delivered unrouted.
ROUTING_TIMEOUT_SECONDS: Final[float] = 10.0
# Messages of recent conversation (user and agent, newest last) Jev reads beside the new message.
CONTEXT_MESSAGE_COUNT: Final[int] = 6
# Each context message is cut to its last this-many words, so one long reply cannot crowd out the rest.
_CONTEXT_MESSAGE_WORD_LIMIT: Final[int] = 300
# The newest transcript events read to find those messages; events are payload-free, so this many
# comfortably holds CONTEXT_MESSAGE_COUNT messages between tool calls.
_CONTEXT_TAIL_EVENT_LIMIT: Final[int] = 200
# How likely Jev must judge a yes before the message is routed.
_YES_CUTOFF: Final[float] = 0.5
# A just-created quick-change chat is not taking messages until its agent is up.
_DELIVERY_ATTEMPTS: Final[int] = 36
_DELIVERY_RETRY_SECONDS: Final[float] = 5.0

QUICK_CHANGE_QUESTION: Final[str] = "quick_look_and_feel_change"
JEV_QUESTIONS: Final[dict[str, dict[str, Any]]] = {
    QUICK_CHANGE_QUESTION: {
        "type": "noul",
        "instructions": (
            "A user sent new_message to an AI agent that builds and runs apps on their computer. Is new_message "
            "a request for a quick look-and-feel change to an existing app: a colour, font or font size, "
            "spacing, showing or hiding an element, light or dark styling, or restyling a page, that can be "
            "made without new features, new data or backend work?"
        ),
        "criteria": {
            "true": (
                "A quick visual or styling change to an existing app, and nothing more: no new feature, "
                "no new data, no backend or behaviour change."
            ),
            "false": (
                "Anything else: building a new app, adding a feature or data, fixing behaviour, a question, "
                "conversation, or a styling request bundled with other work."
            ),
        },
    },
}

ROUTING_NOTE: Final[str] = (
    "Routed to the quick look-and-feel agent, which is making this change; do not make it yourself. "
    "Its short report will arrive in this chat as a message when it is done."
)


class QuickChangeDeliveryError(ChatAppError):
    """A message could not be delivered to a chat (refused by the send path)."""


class JevCredentials(FrozenModel):
    """Where Jev is asked and the key that endpoint accepts."""

    endpoint_url: str = Field(description="The URL Jev's requests are posted to")
    api_key: str = Field(description="The proxy's LiteLLM virtual key, or an OpenRouter key", repr=False)
    is_openrouter_key: bool = Field(
        default=False, description="Whether this is the saved OpenRouter key rather than a proxy sign-in"
    )


class JevOutcomeStatus(LowerCaseStrEnum):
    ANSWERED = auto()
    # Jev is not there for this chat (no route, or a refused key).
    UNAVAILABLE = auto()
    # A timeout, a network error, or an answer that cannot be read.
    FAILED = auto()


class JevOutcome(FrozenModel):
    status: JevOutcomeStatus = Field(description="How the call ended")
    response: dict[str, Any] | None = Field(default=None, description="Jev's whole answer when it answered")
    detail: str = Field(default="", description="What went wrong, for the log")


class RoutingDecision(LowerCaseStrEnum):
    """What happened to one message."""

    ROUTED = auto()
    NOT_ROUTED = auto()
    NO_CREDENTIALS = auto()
    JEV_UNAVAILABLE = auto()
    JEV_FAILED = auto()
    # Jev answered, but without a usable answer to the question.
    UNUSABLE_ANSWER = auto()


class RoutingResult(FrozenModel):
    """What one message's routing came to."""

    text: str = Field(description="What the chat's own agent receives: the message, marked when routed")
    is_routed: bool = Field(default=False, description="Whether the quick-change agent should make the change")
    user_message: str = Field(default="", description="The user's words, forwarded as they are (routed only)")
    recent_conversation: str = Field(default="", description="The context Jev read, forwarded too (routed only)")


# Answers that mean Jev is not there for this chat.
_UNAVAILABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({401, 403, 404})


@pure
def resolve_jev_credentials(managed_env: Mapping[str, str]) -> JevCredentials | None:
    """The proxy credentials in one account's managed env, or None unless it is signed in to an Imbue proxy."""
    api_key = managed_env.get(ANTHROPIC_API_KEY_ENV_VAR, "")
    base_url = managed_env.get(ANTHROPIC_BASE_URL_ENV_VAR, "").rstrip("/")
    if api_key == "" or base_url == "":
        return None
    return JevCredentials(endpoint_url=f"{base_url}{JEV_ROUTE_PATH}", api_key=api_key)


def read_openrouter_jev_credentials(key_path: Path) -> JevCredentials | None:
    """OpenRouter credentials from the saved key file, or None when there is no key in it."""
    try:
        api_key = key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Ignoring an unreadable OpenRouter key file at {}: {}", key_path, e)
        return None
    if api_key == "":
        return None
    return JevCredentials(endpoint_url=OPENROUTER_JEV_URL, api_key=api_key, is_openrouter_key=True)


def read_chat_jev_credentials(binding: ChatAccountBinding | None, openrouter_key_path: Path) -> JevCredentials | None:
    """The saved OpenRouter key when there is one, else the chat's proxy sign-in (its account's, else the workspace's)."""
    openrouter_credentials = read_openrouter_jev_credentials(openrouter_key_path)
    if openrouter_credentials is not None:
        return openrouter_credentials
    if binding is not None:
        account_settings = binding.account_dir / "settings.json"
        if account_settings.is_file():
            credentials = resolve_jev_credentials(read_managed_auth_env(settings_path_override=account_settings))
            if credentials is not None:
                return credentials
    return resolve_jev_credentials(read_managed_auth_env())


def ask_jev(
    credentials: JevCredentials, jev_request: dict[str, Any], http_client: httpx.Client, timeout_seconds: float
) -> JevOutcome:
    """One call to Jev, through the proxy or straight to OpenRouter."""
    try:
        response = http_client.post(
            credentials.endpoint_url,
            json=jev_request,
            headers={"Authorization": f"Bearer {credentials.api_key}"},
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as e:
        return JevOutcome(status=JevOutcomeStatus.FAILED, detail=f"request failed: {type(e).__name__}: {e}")
    if response.status_code in _UNAVAILABLE_STATUS_CODES:
        return JevOutcome(status=JevOutcomeStatus.UNAVAILABLE, detail=f"HTTP {response.status_code}")
    if response.status_code != 200:
        return JevOutcome(status=JevOutcomeStatus.FAILED, detail=f"HTTP {response.status_code}: {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as e:
        return JevOutcome(status=JevOutcomeStatus.FAILED, detail=f"unreadable answer: {e}")
    if not isinstance(payload, dict):
        return JevOutcome(status=JevOutcomeStatus.FAILED, detail="answer is not an object")
    return JevOutcome(status=JevOutcomeStatus.ANSWERED, response=payload)


@pure
def _normalize_text(text: str) -> str:
    return " ".join(text.split())


@pure
def _event_text(event: Mapping[str, Any]) -> str:
    """The words of a user or assistant event as the conversation shows them."""
    if event.get("type") == "assistant_message":
        return str(event.get("text") or "")
    body = event.get("display_body")
    if isinstance(body, str) and body:
        return body
    return str(event.get("content") or "")


@pure
def _is_user_prompt_event(event: Mapping[str, Any]) -> bool:
    """Whether an event is a message the user wrote (a framework line, chip, notice or status is not)."""
    if event.get("type") != "user_message" or event.get("role") != "user":
        return False
    display = event.get("display")
    return display is None or display == DisplayKind.PROMPT_WITH_CONTEXT


@pure
def build_recent_conversation(events: Sequence[Mapping[str, Any]], new_message: str, message_count: int) -> str:
    """The last ``message_count`` user messages and agent replies before a new message, oldest first.

    Tool calls and framework lines are left out. The new message itself is skipped if it has already
    reached the transcript (the newest user message carrying its text). Each message keeps only its
    last ``_CONTEXT_MESSAGE_WORD_LIMIT`` words.
    """
    new_message_text = _normalize_text(new_message)
    is_new_message_skipped = new_message_text == ""
    lines: list[str] = []
    for event in reversed(events):
        if len(lines) >= message_count:
            break
        if _is_user_prompt_event(event):
            if not is_new_message_skipped and new_message_text in _normalize_text(_event_text(event)):
                is_new_message_skipped = True
                continue
            speaker = "User"
        elif event.get("type") == "assistant_message":
            speaker = "Agent"
        else:
            continue
        words = _event_text(event).split()
        if not words:
            continue
        lines.append(f"{speaker}: {' '.join(words[-_CONTEXT_MESSAGE_WORD_LIMIT:])}")
    return "\n\n".join(reversed(lines))


@pure
def build_jev_request(new_message: str, recent_conversation: str) -> dict[str, Any]:
    return {
        "model": JEV_MODEL,
        "state": {"new_message": new_message, "recent_conversation": recent_conversation},
        "questions": dict(JEV_QUESTIONS),
    }


@pure
def read_quick_change_probability(response: Mapping[str, Any]) -> float | None:
    """How likely Jev judged the message a quick look-and-feel change, or None for an answer without it."""
    answers = response.get("answers")
    if not isinstance(answers, Mapping):
        return None
    answer = answers.get(QUICK_CHANGE_QUESTION)
    if not isinstance(answer, Mapping):
        return None
    probability = answer.get("noul")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        return None
    return float(probability)


@pure
def decide_routing(outcome: JevOutcome, probability: float | None) -> RoutingDecision:
    """The routing decision one Jev call comes to."""
    if outcome.status is JevOutcomeStatus.UNAVAILABLE:
        return RoutingDecision.JEV_UNAVAILABLE
    if outcome.status is JevOutcomeStatus.FAILED:
        return RoutingDecision.JEV_FAILED
    if probability is None:
        return RoutingDecision.UNUSABLE_ANSWER
    return RoutingDecision.ROUTED if probability > _YES_CUTOFF else RoutingDecision.NOT_ROUTED


@pure
def is_routable_message(message: str) -> bool:
    """Whether a sent text is the user's own words (a report, notice, slash command or chip is not)."""
    if _normalize_text(message) == "" or message.lstrip().startswith("/"):
        return False
    return classify_user_message(message) is None


@pure
def mark_routed_message(message: str) -> str:
    """The message as the chat's own agent receives it once routed: the routing note, then the user's words."""
    return f"<{QUICK_CHANGE_ROUTING_NOTE_TAG}>{ROUTING_NOTE}</{QUICK_CHANGE_ROUTING_NOTE_TAG}>\n{message}"


@pure
def quick_change_agent_message(main_chat_id: str, user_message: str, recent_conversation: str) -> str:
    """What the quick-change agent receives for one routed message: the skill, where to report, and the request."""
    return (
        f"/{QUICK_CHANGE_SKILL}\n\n"
        f"Main chat: {main_chat_id}\n\n"
        "Recent conversation in the main chat, for context (oldest first):\n"
        f"{recent_conversation or '(none)'}\n\n"
        "The user's request, word for word:\n"
        f"{user_message}"
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QuickChangeRouter(MutableModel):
    """Asks Jev about each sent message and forwards quick look-and-feel changes to the quick-change agent."""

    model_config = {"arbitrary_types_allowed": True}

    data_dir: Path = Field(frozen=True, description="The chat app's data folder (routing log, agent record)")
    resolve_credentials: Callable[[ChatId | None], JevCredentials | None] = Field(
        frozen=True, description="The Jev credentials a chat's calls use, or None when it has none"
    )
    get_conversation_tail: Callable[[ChatId, int], list[dict[str, Any]]] = Field(
        frozen=True, description="The newest events (up to the limit) of the agent the chat runs on, oldest first"
    )
    call_jev: Callable[[JevCredentials, dict[str, Any], float], JevOutcome] = Field(
        frozen=True, description="One call to Jev, with its timeout in seconds"
    )
    create_agent_chat: Callable[[str, str, dict[str, str]], str] = Field(
        frozen=True, description="Creates the quick-change chat from a name, first message and labels; returns its id"
    )
    deliver: Callable[[ChatId, str], bool] = Field(
        frozen=True,
        description="Sends a message into a chat through the send path; False when the chat is gone, "
        "QuickChangeDeliveryError when it refuses",
    )
    timeout_seconds: float = Field(default=ROUTING_TIMEOUT_SECONDS, frozen=True, description="Jev's time limit")
    context_message_count: int = Field(default=CONTEXT_MESSAGE_COUNT, frozen=True, ge=0)
    run_in_background: Callable[[Callable[[], None]], None] = Field(
        default=lambda fn: threading.Thread(target=fn, name="quick-change-forward", daemon=True).start(),
        frozen=True,
        description="Where the forward runs, off the send's thread",
    )
    wait: Callable[[float], object] = Field(
        default_factory=lambda: threading.Event().wait, frozen=True, description="Waits between delivery attempts"
    )

    _agent_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _log_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def quick_change_chat_id(self) -> str | None:
        """The quick-change agent's chat id, or None before its first use."""
        path = self.data_dir / QUICK_CHANGE_AGENT_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning("Ignoring an unreadable quick-change agent record at {}: {}", path, e)
            return None
        chat_id = payload.get("chat_id") if isinstance(payload, dict) else None
        return chat_id if isinstance(chat_id, str) and chat_id else None

    def route(self, chat_id: ChatId | None, message: str) -> RoutingResult:
        """Ask Jev about ``message`` and say what the chat's own agent should receive.

        ``chat_id`` is None for a chat's first message sent with its create, before the chat has an id.
        Never raises: anything that goes wrong leaves the message untouched.
        """
        unrouted = RoutingResult(text=message)
        if not is_routable_message(message) or (chat_id is not None and str(chat_id) == self.quick_change_chat_id()):
            return unrouted
        try:
            return self._route(chat_id, message)
        except (OSError, ValueError, httpx.HTTPError) as e:
            logger.opt(exception=e).error("Quick-change routing failed for chat {}; delivering unrouted", chat_id)
            return unrouted

    def forward(self, main_chat_id: ChatId, result: RoutingResult) -> None:
        """Hand a routed message to the quick-change agent, off the caller's thread; nothing for an unrouted one."""
        if not result.is_routed:
            return
        text = quick_change_agent_message(str(main_chat_id), result.user_message, result.recent_conversation)
        self.run_in_background(lambda: self._forward(main_chat_id, text))

    def _route(self, chat_id: ChatId | None, message: str) -> RoutingResult:
        started = time.monotonic()
        record: dict[str, Any] = {
            "event": "jev_decision",
            "chat_id": None if chat_id is None else str(chat_id),
            "message": message,
            "started_at": _now_iso(),
        }
        credentials = self.resolve_credentials(chat_id)
        if credentials is None:
            self._log({**record, "decision": str(RoutingDecision.NO_CREDENTIALS), "finished_at": _now_iso()})
            return RoutingResult(text=message)
        tail = [] if chat_id is None else self.get_conversation_tail(chat_id, _CONTEXT_TAIL_EVENT_LIMIT)
        recent_conversation = build_recent_conversation(tail, message, self.context_message_count)
        jev_request = build_jev_request(message, recent_conversation)
        record.update(
            jev_endpoint=credentials.endpoint_url,
            is_openrouter_key=credentials.is_openrouter_key,
            jev_request=jev_request,
            jev_sent_at=_now_iso(),
        )
        outcome = self.call_jev(credentials, jev_request, self.timeout_seconds)
        jev_answered_at = _now_iso()
        probability = read_quick_change_probability(outcome.response) if outcome.response is not None else None
        decision = decide_routing(outcome, probability)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        self._log(
            {
                **record,
                "jev_answered_at": jev_answered_at,
                "jev_outcome": outcome.model_dump(mode="json"),
                "probability": probability,
                "decision": str(decision),
                "elapsed_ms": elapsed_ms,
                "finished_at": _now_iso(),
            }
        )
        logger.info("Quick-change routing for chat {}: {} ({} ms, p={})", chat_id, decision, elapsed_ms, probability)
        if decision is not RoutingDecision.ROUTED:
            return RoutingResult(text=message)
        return RoutingResult(
            text=mark_routed_message(message),
            is_routed=True,
            user_message=message,
            recent_conversation=recent_conversation,
        )

    def _forward(self, main_chat_id: ChatId, text: str) -> None:
        """Hand the request to the quick-change agent, creating it on first use; logged, never raised."""
        started = time.monotonic()
        try:
            quick_chat_id, is_created = self._deliver_to_agent(text)
        except (AgentCreationError, ChatAppError, OSError) as e:
            logger.opt(exception=e).error(
                "Could not forward chat {}'s message to the quick-change agent", main_chat_id
            )
            self._log(
                {
                    "event": "forward_failed",
                    "chat_id": str(main_chat_id),
                    "error": f"{type(e).__name__}: {e}",
                    "finished_at": _now_iso(),
                }
            )
            return
        self._log(
            {
                "event": "forwarded",
                "chat_id": str(main_chat_id),
                "quick_change_chat_id": quick_chat_id,
                "is_agent_created": is_created,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "finished_at": _now_iso(),
            }
        )

    def _deliver_to_agent(self, text: str) -> tuple[str, bool]:
        """Deliver to the existing quick-change chat, or create it with ``text`` as its first message."""
        with self._agent_lock:
            existing = self.quick_change_chat_id()
            if existing is not None and self._deliver_with_retries(ChatId(existing), text):
                return existing, False
            created = self.create_agent_chat(QUICK_CHANGE_CHAT_NAME, text, {QUICK_CHANGE_LABEL: "true"})
            self._write_agent_record(created)
            logger.info("Created the quick-change chat {}", created)
            return created, True

    def _deliver_with_retries(self, chat_id: ChatId, text: str) -> bool:
        """True once delivered (or held for a chat coming up), False when the chat is gone."""
        for attempt in range(1, _DELIVERY_ATTEMPTS + 1):
            try:
                return self.deliver(chat_id, text)
            except QuickChangeDeliveryError as e:
                if attempt == _DELIVERY_ATTEMPTS:
                    raise
                logger.debug("Quick-change chat {} is not taking messages yet ({}); retrying", chat_id, e)
                self.wait(_DELIVERY_RETRY_SECONDS)
        return False

    def _write_agent_record(self, chat_id: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / QUICK_CHANGE_AGENT_FILENAME
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps({"chat_id": chat_id, "created_at": _now_iso()}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _log(self, record: dict[str, Any]) -> None:
        with self._log_lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with (self.data_dir / ROUTING_LOG_FILENAME).open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
