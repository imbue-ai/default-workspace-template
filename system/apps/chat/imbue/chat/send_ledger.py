"""Each send's answer by its send-time id, so a send the page repeats is never delivered twice.

A page whose request got no answer (the connection dropped, a proxy gave up on a long send) cannot
tell whether the chat app delivered the message, but the chat app can. The page sends it again
with the same id, marked as a repeat; the ledger hands the repeat the first send's answer, waiting
for it while it is still under way, instead of delivering it again. A repeat gets that answer even
when it is a refusal, since a refused send can still have been typed into the agent (mngr could not
confirm it). A send under the same id that is not a repeat (the user's Retry or Send again) gets a
taken send's answer too, but after a refusal it is sent afresh.
"""

import threading
import time
from collections.abc import Callable
from typing import Final

from flask import Response
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

# How long an answered send is remembered: far longer than a page keeps sending one again.
_ANSWER_KEPT_SECONDS: Final[float] = 15 * 60.0
# How long a repeat waits for the send it repeats; a send settles well within it.
_REPEAT_WAIT_SECONDS: Final[float] = 15 * 60.0


class SendStillUnderWayError(Exception):
    """A repeated send outwaited the send it repeats."""


class _Answer(MutableModel):
    data: bytes = Field(description="The first send's response body")
    status: int = Field(description="The first send's HTTP status")
    answered_at: float = Field(description="When it was answered (monotonic seconds)")

    @property
    def is_taken(self) -> bool:
        return self.status < 400


class _Entry(MutableModel):
    model_config = {"arbitrary_types_allowed": True}

    settled: threading.Event = Field(default_factory=threading.Event, description="Set once the send has an outcome")
    answer: _Answer | None = Field(default=None, description="The send's answer, taken or refused, once it has one")


class SendLedger(MutableModel):
    """The sends under way and the ones answered lately, by chat and send-time id."""

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _entries: dict[str, _Entry] = PrivateAttr(default_factory=dict)

    def send_once(
        self,
        key: str,
        send: Callable[[], Response],
        replay: Callable[[bytes, int], Response],
        is_repeat: bool,
    ) -> Response:
        """Run ``send`` for ``key`` unless a send for it is under way or answered; then answer as that one did.

        ``is_repeat`` is the page sending again a request that got no answer: it gets the answer of the send it
        repeats, a refusal included. Any other send gets a taken send's answer, and after a refusal is sent afresh.
        ``replay`` rebuilds the answer, from its body and status, for the request repeating it.
        """
        with self._lock:
            self._forget_old_answers_locked()
            entry = self._entries.get(key)
            if entry is not None and not is_repeat and entry.answer is not None and not entry.answer.is_taken:
                entry = None
            is_first = entry is None
            if entry is None:
                entry = _Entry()
                self._entries[key] = entry
        if is_first:
            return self._send_first(key, entry, send)
        if not entry.settled.wait(timeout=_REPEAT_WAIT_SECONDS):
            raise SendStillUnderWayError(f"The send {key} is still on its way")
        if entry.answer is not None:
            return replay(entry.answer.data, entry.answer.status)
        # The send this one waited on raised before it had an answer, so this one is a send of its own.
        return self.send_once(key, send, replay, is_repeat)

    def _send_first(self, key: str, entry: _Entry, send: Callable[[], Response]) -> Response:
        try:
            response = send()
            entry.answer = _Answer(data=response.get_data(), status=response.status_code, answered_at=time.monotonic())
            return response
        finally:
            if entry.answer is None:
                with self._lock:
                    if self._entries.get(key) is entry:
                        del self._entries[key]
            entry.settled.set()

    def _forget_old_answers_locked(self) -> None:
        cutoff = time.monotonic() - _ANSWER_KEPT_SECONDS
        stale = [
            key
            for key, entry in self._entries.items()
            if entry.answer is not None and entry.answer.answered_at < cutoff
        ]
        for key in stale:
            del self._entries[key]
