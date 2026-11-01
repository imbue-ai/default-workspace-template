"""What the chat app knows of the people and agents in the workspace: the shell's roster, asked over loopback.

The shell owns names, keys, and handles (the multiplayer spec, S15); this app reads them from it rather than from
its files. The document is cached briefly, since every send and every page asks, and an unreachable shell leaves
the last document standing (or none), with the sender's own header as the fallback for a name.
"""

import threading
import time
from collections.abc import Callable
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from workspace_layout.errors import ShellOpError
from workspace_layout.identity import RequestIdentity
from workspace_layout.interfaces import ShellLayoutInterface
from workspace_layout.primitives import ParticipantKey
from workspace_layout.roster import RosterDocument
from workspace_layout.roster import RosterParticipant

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# How long a roster is trusted before the shell is asked again: a rename or a new grant reaches a send within this.
ROSTER_CACHE_SECONDS: Final[float] = 5.0


@pure
def fallback_display_name(key: ParticipantKey, identity: RequestIdentity) -> str:
    """What to call a sender the roster does not name: a visiting agent's grant name, a person's email local part,
    else the key itself."""
    if identity.agent_name:
        return identity.agent_name
    if identity.email and "@" in identity.email:
        local_part = identity.email.split("@", 1)[0]
        if local_part:
            return local_part
    return str(key)


class RosterClient(MutableModel):
    """The shell's roster, cached for a few seconds."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    shell: ShellLayoutInterface = Field(frozen=True, description="The shell the roster is asked of")
    cache_seconds: float = Field(default=ROSTER_CACHE_SECONDS, frozen=True, description="How long one answer lasts")
    clock: Callable[[], float] = Field(default=time.monotonic, frozen=True, description="Monotonic seconds")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _document: RosterDocument | None = PrivateAttr(default=None)
    _fetched_at: float | None = PrivateAttr(default=None)

    def document(self) -> RosterDocument:
        """The roster as the shell last answered it; empty when it has never answered."""
        with self._lock:
            now = self.clock()
            if (
                self._fetched_at is not None
                and now - self._fetched_at < self.cache_seconds
                and self._document is not None
            ):
                return self._document
            try:
                self._document = self.shell.roster()
            except ShellOpError as e:
                logger.debug("Could not read the roster from the shell; keeping the last one: {}", e)
            self._fetched_at = now
            return self._document if self._document is not None else RosterDocument(participants=())

    def participant(self, key: ParticipantKey) -> RosterParticipant | None:
        for entry in self.document().participants:
            if entry.key == key:
                return entry
        return None

    def display_name_of(self, key: ParticipantKey, identity: RequestIdentity) -> str:
        """The sender's name: the roster's, else what their header says."""
        entry = self.participant(key)
        return entry.display_name if entry is not None else fallback_display_name(key, identity)
