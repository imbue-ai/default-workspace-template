"""Intakes: text entering a chat from outside a chat page (post-launch-paths plan sections 3.5 and 3.6).

The intake route decides which chat receives a text and whether it is sent or drafted. What
the server can finish it finishes; what needs a page (a draft into a composer, a choice the
user has to make, a first message that needs an account signed in) is held here as a pending
intake under a one-time token, which the chat root applies once its window lands on the path
that carries it.
"""

import secrets
import threading
import time
from collections.abc import Callable
from collections.abc import Sequence
from typing import Final
from urllib.parse import parse_qs
from urllib.parse import urlencode
from urllib.parse import urlsplit

from pydantic import Field
from pydantic import PrivateAttr

from imbue.chat.models import ChatSnapshot
from imbue.chat.models import IntakeRequest
from imbue.chat.models import parse_subagent_key
from imbue.chat.primitives import AGENT_ID_PATTERN
from imbue.chat.primitives import ChatId
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# The query parameters of the chat root's URL: the selected chat, and a pending intake to apply.
CHAT_QUERY_KEY: Final[str] = "chat"
INTAKE_QUERY_KEY: Final[str] = "intake"

# How long a pending intake waits to be applied before it is dropped.
PENDING_INTAKE_TTL_SECONDS: Final[float] = 15 * 60.0


@pure
def chat_id_selected_by_window_path(path: str) -> ChatId | None:
    """The chat a chat-app window path shows: the root's ``/?chat=<id>``, a chat page's ``/<id>``, or a subagent
    view's ``/<id>.<agent>.<session>``; None for any other path."""
    split = urlsplit(path)
    selected = parse_qs(split.query).get(CHAT_QUERY_KEY, [])
    if selected and AGENT_ID_PATTERN.fullmatch(selected[0]):
        return ChatId(selected[0])
    segments = [segment for segment in split.path.split("/") if segment]
    if len(segments) != 1:
        return None
    key = segments[0]
    if AGENT_ID_PATTERN.fullmatch(key):
        return ChatId(key)
    subagent = parse_subagent_key(key)
    return subagent.chat_id if subagent is not None else None


@pure
def most_recently_messaged_chat_id(snapshots: Sequence[ChatSnapshot]) -> ChatId | None:
    """The chat messaged last among the listed ones; a chat never messaged counts as oldest; None with none."""
    if not snapshots:
        return None
    return max(snapshots, key=lambda snapshot: snapshot.last_messaged_at or 0.0).chat_id


@pure
def intake_path(chat_id: ChatId | None, token: str | None) -> str:
    """The root path an intake answers: the selection, with the pending intake's token when one is held."""
    params: dict[str, str] = {}
    if chat_id is not None:
        params[CHAT_QUERY_KEY] = str(chat_id)
    if token is not None:
        params[INTAKE_QUERY_KEY] = token
    return f"/?{urlencode(params)}" if params else "/"


class PendingIntake(FrozenModel):
    """An intake the server could not finish, held for the chat root to apply."""

    request: IntakeRequest = Field(description="The intake as it arrived")
    chat_id: ChatId | None = Field(description="The chat it resolved to; None for a choice the user makes")
    needs_pick: bool = Field(description="Whether the root has to offer the picker before applying")
    minted_at: float = Field(description="Wall-clock epoch seconds the intake was held at, for expiry")


class PendingIntakeStore(MutableModel):
    """The pending intakes by token, in memory; a token is applied at most once and expires unapplied."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    clock: Callable[[], float] = Field(default=time.time, frozen=True, description="Wall-clock epoch seconds")
    ttl_seconds: float = Field(
        default=PENDING_INTAKE_TTL_SECONDS, frozen=True, description="How long an unapplied intake is kept"
    )

    _by_token: dict[str, PendingIntake] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def mint(self, request: IntakeRequest, chat_id: ChatId | None, needs_pick: bool) -> str:
        """Hold an intake and answer its one-time token."""
        token = secrets.token_urlsafe(16)
        pending = PendingIntake(request=request, chat_id=chat_id, needs_pick=needs_pick, minted_at=self.clock())
        with self._lock:
            self._expire_locked()
            self._by_token[token] = pending
        return token

    def get(self, token: str) -> PendingIntake | None:
        """The held intake, or None once applied, discarded, expired, or never minted."""
        with self._lock:
            self._expire_locked()
            return self._by_token.get(token)

    def take(self, token: str) -> PendingIntake | None:
        """The held intake, consumed: a second take of the same token answers None."""
        with self._lock:
            self._expire_locked()
            return self._by_token.pop(token, None)

    def discard(self, token: str) -> None:
        with self._lock:
            self._by_token.pop(token, None)

    def _expire_locked(self) -> None:
        now = self.clock()
        expired = [token for token, pending in self._by_token.items() if now - pending.minted_at > self.ttl_seconds]
        for token in expired:
            del self._by_token[token]
