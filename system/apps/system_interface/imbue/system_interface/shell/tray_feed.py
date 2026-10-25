"""The tray's feed from the chat app (the multiplayer spec, sections 6.4 and 6.5): the speech bubble each message
raises over its speaker's tray entry, and what every chat is doing, which the agent's card lists.

The chat app posts both over loopback. A bubble is broadcast to every window but the speaker's own clients (the
clients whose records carry the speaker's key; the owner's clients carry the synthetic ``owner`` key until the
workspace is shared, so a bubble of the owner's skips those too). The chats' activity is kept, so a window that
connects later paints the agent's card from the last report rather than waiting for the next chat-list change.
"""

import threading
from collections.abc import Sequence

from pydantic import PrivateAttr
from workspace_layout.feed import ChatsActivityReport
from workspace_layout.primitives import OWNER_PARTICIPANT_KEY
from workspace_layout.primitives import ParticipantKey
from workspace_layout.records import ClientRecord

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure


@pure
def speakers_client_ids(speaker: ParticipantKey, is_owner: bool, clients: Sequence[ClientRecord]) -> set[str]:
    """The clients a bubble of ``speaker``'s is not sent to: the ones recorded as theirs, and, for the owner, the
    ones recorded under the synthetic owner key from before the workspace was shared."""
    own_keys = {speaker, OWNER_PARTICIPANT_KEY} if is_owner else {speaker}
    return {str(client.id) for client in clients if client.participant in own_keys}


class ChatsActivityStore(MutableModel):
    """The last chats-activity report the chat app posted, for the windows that connect after it."""

    model_config = {"extra": "forbid", "frozen": False}

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _report: ChatsActivityReport | None = PrivateAttr(default=None)

    def current(self) -> ChatsActivityReport | None:
        with self._lock:
            return self._report

    def replace(self, report: ChatsActivityReport) -> None:
        with self._lock:
            self._report = report
