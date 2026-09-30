"""The spare chat agents: a small pool kept started on the account a new chat would get, so a new chat starts at once.

Starting a chat's agent (``mngr create``: provisioning, the harness booting, its readiness
signal) takes seconds, and a first message sent into a chat that is still starting waits behind
it ("Connecting..."). So the chat app keeps a pool of agents already started on the terms the
next new chat would get (``SpareChatTerms``), hidden from every listing, and hands one to each
``create_chat`` those terms fit: a booted one when there is one, else one still booting, which
is still ahead of a create of its own. The pool is topped up as spares are taken. A spare's id
is minted as the id of the chat it will become (a chat's id is its first agent's), so
``MINDS_CHAT_ID`` and every label baked in at its create are already right when it is handed
over, and the hand-over itself runs no mngr command.

The spares are recorded in ``data/.state/chat/spare_chat.json`` before their create starts, so
the observe stream, which lists an agent as soon as mngr provisions it, never shows one as a
chat, and a restart of this app still knows them: a ready spare stays the spare, and a create
the restart cut short is destroyed.
"""

import json
import os
import threading
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.chat.primitives import ChatId
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

SPARE_CHAT_FILENAME: Final[str] = "spare_chat.json"
_SPARES_KEY: Final[str] = "spares"


class SpareChatPhase(UpperCaseStrEnum):
    """Where a spare agent is in its life."""

    # Its ``mngr create`` is running, or its harness has not said it accepts input yet.
    CREATING = auto()
    # Its harness accepts input; waiting for a new chat to take it.
    READY = auto()
    # Taken by a new chat while still being created: it becomes that chat once its harness is up.
    CLAIMED = auto()
    # No longer wanted (its terms went stale, its process died, or its create failed); its
    # ``mngr destroy`` is due or running.
    DISCARDING = auto()


class SpareChatTerms(FrozenModel):
    """What a new chat's agent is started with that a spare has to match to be handed to it."""

    account_id: str = Field(description="The account the agent is bound to")
    project_label: str = Field(description="The agent's project label; empty for none")
    is_fast: bool = Field(description="Whether the agent is started in fast mode")


class SpareChatAgent(FrozenModel):
    """One spare agent: the chat it will become, the terms it was started on, and where it is in its life."""

    chat_id: ChatId = Field(description="The spare's agent id, which is the id of the chat it becomes")
    display_name: str = Field(description="The name it was created with, reserved like any chat's")
    terms: SpareChatTerms = Field(description="What it was started with")
    phase: SpareChatPhase = Field(description="Where it is in its life")

    def with_phase(self, phase: SpareChatPhase) -> "SpareChatAgent":
        return self.model_copy_update(to_update(self.field_ref().phase, phase))


class SpareChatStore(MutableModel):
    """The spares file: read by the live chat at build and by a secondary on every sweep, written whole on every change."""

    path: Path = Field(frozen=True, description="The spares file")
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def read(self) -> tuple[SpareChatAgent, ...]:
        """The spares as recorded; an absent file reads as none, an unreadable one as none with a warning."""
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("Ignored an unreadable spare chat file at {}: {}", self.path, e)
            return ()
        entries = payload.get(_SPARES_KEY) if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            logger.warning("Ignored a spare chat file of the wrong shape at {}", self.path)
            return ()
        try:
            return tuple(SpareChatAgent.model_validate(entry) for entry in entries)
        except ValidationError as e:
            logger.warning("Ignored a spare chat file at {} that does not fit: {}", self.path, e)
            return ()

    def write(self, spares: Sequence[SpareChatAgent]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(".json.tmp")
            payload = {_SPARES_KEY: [spare.model_dump(mode="json") for spare in spares]}
            temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp_path, self.path)


@pure
def spares_after_restart(spares: Sequence[SpareChatAgent]) -> tuple[SpareChatAgent, ...]:
    """The spares a build starts from: a create that was running when this app stopped did not finish
    here, so its agent (made or half-made) is discarded rather than trusted, and so is one a chat had
    claimed, whose chat went with the provisional record this app held in memory."""
    return tuple(
        spare.with_phase(SpareChatPhase.DISCARDING)
        if spare.phase in (SpareChatPhase.CREATING, SpareChatPhase.CLAIMED)
        else spare
        for spare in spares
    )


@pure
def pooled_spares(spares: Sequence[SpareChatAgent]) -> tuple[SpareChatAgent, ...]:
    """The spares that count toward the pool: being created or waiting to be taken."""
    return tuple(spare for spare in spares if spare.phase in (SpareChatPhase.CREATING, SpareChatPhase.READY))
