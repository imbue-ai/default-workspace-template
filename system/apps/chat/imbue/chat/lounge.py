"""The Lounge (the multiplayer spec, section 7.5): the chat a click on a person in the desktop's tray opens, with a
mention of them drafted into its composer. It is the chat titled "Lounge" of either kind, found by title alone: the
room this keeper makes, or what that room became (an agent chat, through the switch of section 7.6), or a chat the
user named so.

A room so titled is made the first time the roster holds anyone besides the owner (a person, an invited email, or a
visiting agent), from the roster the shell answers, and again whenever something needs the Lounge after it was deleted
or renamed (a click on a person), only when no chat of any kind carries the title; two needs at once make one. A
workspace nobody is invited into has none.
"""

import threading
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from workspace_layout.primitives import ParticipantKind
from workspace_layout.roster import RosterDocument

from imbue.chat.agent_manager import AgentManager
from imbue.chat.models import AgentCreationError
from imbue.chat.models import ChatSnapshot
from imbue.chat.primitives import ChatId
from imbue.chat.primitives import ChatKind
from imbue.chat.roster_client import RosterClient
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

LOUNGE_TITLE: Final[str] = "Lounge"
# How often the roster is read again for an invitation nothing else has reported (a page's read or a send reads it
# sooner).
ROSTER_POLL_INTERVAL_SECONDS: Final[float] = 60.0


@pure
def find_lounge(snapshots: Sequence[ChatSnapshot]) -> ChatId | None:
    """The chat titled "Lounge", of either kind, when there is one."""
    for snapshot in snapshots:
        if snapshot.title == LOUNGE_TITLE:
            return snapshot.chat_id
    return None


@pure
def has_anyone_besides_the_owner(document: RosterDocument) -> bool:
    """Whether anyone was invited into the workspace: a person, an invited email, or a visiting agent."""
    return any(
        entry.kind in (ParticipantKind.PERSON, ParticipantKind.INVITED, ParticipantKind.VISITING_AGENT)
        and not entry.is_owner
        for entry in document.participants
    )


class LoungeKeeper(MutableModel):
    """Makes the Lounge, a room, when it is needed and no chat of any kind carries its title."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    agent_manager: AgentManager = Field(frozen=True, description="The chats, and how a room is created")
    poll_interval_seconds: float = Field(
        default=ROSTER_POLL_INTERVAL_SECONDS, frozen=True, description="How often the roster is read again"
    )

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    # The Lounge this keeper created, for as long as it is provisional: a chat being created is not listed yet,
    # and a second create of the name would be refused. Once listed, its title says whether it is still the Lounge.
    _created: ChatId | None = PrivateAttr(default=None)

    def ensure(self) -> ChatId:
        """The Lounge's chat id: the chat so titled of either kind, else a room created under the title; raises what a
        room's create raises."""
        with self._lock:
            found = find_lounge(self.agent_manager.get_chat_snapshots())
            if found is not None:
                return found
            if self._created is not None and self.agent_manager.get_provisional_chat(str(self._created)) is not None:
                return self._created
            self._created = self.agent_manager.create_chat(LOUNGE_TITLE, kind=ChatKind.ROOM).chat_id
            return self._created

    def consider_roster(self, document: RosterDocument) -> None:
        """What a fresh roster means for the Lounge: made once someone besides the owner is in it."""
        if not self.agent_manager.is_agent_list_known() or not has_anyone_besides_the_owner(document):
            return
        try:
            self.ensure()
        except (AgentCreationError, OSError, ValueError) as e:
            logger.warning("Could not make the Lounge for the people invited into the workspace: {}", e)

    def start(self, roster: RosterClient) -> None:
        """Read the roster every ``poll_interval_seconds`` (through its cache, so a fresh read tells this keeper),
        for an invitation nothing else reported."""
        if self._thread is not None:
            return
        thread = threading.Thread(target=self._poll, args=(roster,), daemon=True, name="lounge-keeper")
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _poll(self, roster: RosterClient) -> None:
        while not self._stop.wait(timeout=self.poll_interval_seconds):
            roster.document()
