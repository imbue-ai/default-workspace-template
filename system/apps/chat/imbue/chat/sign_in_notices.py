"""Which chats are refusing turns for want of a signed-in AI account, so the owner hears of each once.

A harness whose account is signed out (an expired subscription login, a revoked API key) answers
every turn with an auth error, which the session parser flags on the reply as ``is_auth_error``.
The chat's own reply shows the sign-in action, but only to someone looking at the chat; the
Imbue Studio app's notification feed is how the owner hears of it otherwise.

A chat's failure is one episode: from its first refused reply to the next reply that was not
refused. Only the start of an episode is news, so a user who keeps typing into a signed-out
chat, or a routine that keeps sending to one, raises one notification rather than one per turn.
"""

import threading
from typing import Any
from typing import Final

from pydantic import PrivateAttr

from imbue.chat.primitives import ChatId
from imbue.imbue_common.mutable_model import MutableModel

# The notification's headline and text. The app titles a chat's notification with the chat's own name, so the
# text says what happened without naming it.
SIGN_IN_NEEDED_TITLE: Final[str] = "Sign in needed"
SIGN_IN_NEEDED_MESSAGE: Final[str] = (
    "Couldn't reply: the AI account this chat uses is signed out. Open the chat to sign in again."
)


class SignInFailureNotices(MutableModel):
    """The chats whose current sign-in failure the owner has already been told of."""

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _failing_chat_ids: set[ChatId] = PrivateAttr(default_factory=set)

    def record_live_events(self, chat_id: ChatId, events: list[dict[str, Any]]) -> bool:
        """Fold a batch of newly parsed events of ``chat_id``; True when it starts a sign-in failure episode
        that is still going at the batch's end (the owner should be told now).

        Only live events belong here: the backlog a watcher primes with is history, and replaying it would
        announce failures the owner may have long since fixed.
        """
        is_episode_started = False
        with self._lock:
            for event in events:
                if event.get("type") != "assistant_message":
                    continue
                if event.get("is_auth_error") is True:
                    if chat_id not in self._failing_chat_ids:
                        self._failing_chat_ids.add(chat_id)
                        is_episode_started = True
                else:
                    self._failing_chat_ids.discard(chat_id)
            return is_episode_started and chat_id in self._failing_chat_ids
