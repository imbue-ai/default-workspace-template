"""The tray's feed from the chat app (the multiplayer spec, sections 6.4 and 6.5): the speech bubble each message
raises over its speaker's tray entry, and what every chat is doing, which the agent's card lists.

The chat app posts both over loopback. A bubble is broadcast to every window but the speaker's own clients: the
clients whose records carry the speaker's key, and for the owner the ones under either of their keys, since their
clients carry the synthetic ``owner`` key until the workspace is shared, a send with no identity (a script's) is
posted under it afterwards, and every reader treats the two as one person (the multiplayer spec, section 4.1). The
chats' activity is kept, so a window that connects later paints the agent's card from the last report rather than
waiting for the next chat-list change.
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
def speakers_client_ids(
    speaker: ParticipantKey, owner_key: ParticipantKey, clients: Sequence[ClientRecord]
) -> set[str]:
    """The clients a bubble of ``speaker``'s is not sent to: the ones recorded as theirs; for the owner, named by
    ``owner_key`` (their account's, once their heartbeats name one) or by the synthetic ``owner`` key, the ones
    recorded under either."""
    own_keys = {speaker}
    if speaker in (owner_key, OWNER_PARTICIPANT_KEY):
        own_keys.update((owner_key, OWNER_PARTICIPANT_KEY))
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
