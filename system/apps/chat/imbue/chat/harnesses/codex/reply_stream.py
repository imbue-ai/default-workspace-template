"""The reply codex is streaming, kept from its app-server's delta notifications.

Codex writes an agent message to its rollout only once the message is finished, while its
app-server streams the text as ``item/agentMessage/delta`` notifications. The chat's live
connection is subscribed to the agent's root thread already (for the message ledger), so its
reader hears every delta whether or not anyone wants a draft; this handler keeps the latest
agent message's text from them, which the reply-draft poller reads for a chat the user is
looking at. Keeping it costs a string append per delta the reader already decoded, and holds
one message at most.

A finished message is kept until the next one starts: codex writes it to the rollout at the
same moment, and the watcher drops the draft once its id is in the transcript, so the reply is
never off the page between the two. A message the turn ended before finishing (an interrupt)
never reaches the rollout, so it is dropped with its turn.
"""

import threading
from collections.abc import Mapping
from typing import Any
from typing import Final

from pydantic import PrivateAttr

from imbue.chat.harnesses.session_watcher import StreamedReply
from imbue.imbue_common.mutable_model import MutableModel

_AGENT_MESSAGE_ITEM_TYPE: Final[str] = "agentMessage"


class CodexReplyStream(MutableModel):
    """The agent message codex is streaming on the agent's root thread, fed one notification at a time.

    The connection's reader thread writes it and the reply-draft poller reads it.
    """

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _message_id: str | None = PrivateAttr(default=None)
    _chunks: list[str] = PrivateAttr(default_factory=list)
    _is_message_finished: bool = PrivateAttr(default=False)

    def handle_notification(self, method: str, params: Mapping[str, Any]) -> None:
        """Fold one notification of the agent's root thread in."""
        if method == "item/agentMessage/delta":
            self._on_delta(params)
        elif method in ("item/started", "item/completed"):
            self._on_item(method, params)
        elif method == "turn/completed":
            self._on_turn_completed()

    def read(self) -> StreamedReply | None:
        """The agent message streamed so far, or None while there is none."""
        with self._lock:
            if self._message_id is None:
                return None
            return StreamedReply(message_id=self._message_id, text="".join(self._chunks))

    def _on_delta(self, params: Mapping[str, Any]) -> None:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str):
            return
        with self._lock:
            if item_id != self._message_id:
                self._start_message_locked(item_id)
            self._chunks.append(delta)

    def _on_item(self, method: str, params: Mapping[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, Mapping) or item.get("type") != _AGENT_MESSAGE_ITEM_TYPE:
            return
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        with self._lock:
            if method == "item/started":
                if item_id != self._message_id:
                    self._start_message_locked(item_id)
                return
            # The finished item carries the whole text: it replaces the deltas, so a delta the
            # reader missed cannot leave a hole in the draft.
            text = item.get("text")
            if item_id == self._message_id or self._message_id is None:
                self._message_id = item_id
                self._chunks = [text] if isinstance(text, str) else self._chunks
                self._is_message_finished = True

    def _on_turn_completed(self) -> None:
        with self._lock:
            if not self._is_message_finished:
                self._message_id = None
                self._chunks = []

    def _start_message_locked(self, item_id: str) -> None:
        self._message_id = item_id
        self._chunks = []
        self._is_message_finished = False
