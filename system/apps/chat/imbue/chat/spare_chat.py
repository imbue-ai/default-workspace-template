"""The spare chat agents: a small pool kept started on the account a new chat would get, so a new chat starts at once.

Starting a chat's agent (``mngr create``: provisioning, the harness booting, its readiness
signal) takes seconds, and a first message sent into a chat that is still starting waits behind
it ("Connecting..."). So the chat app keeps a pool of agents already started on the terms the
next new chat would get (``SpareChatTerms``), hidden from every listing, and hands one to each
``create_chat`` those terms fit: a booted one when there is one, else one still booting, which
is still ahead of a create of its own. The pool is topped up as spares are taken. A spare's id
is minted as the id of the chat it will become (a chat's id is its first agent's), so
``MINDS_CHAT_ID`` and every label baked in at its create are already right when it is handed
over, and the hand-over waits on no mngr command.

A chat minted to await its first send (the one an empty chat list opens on, or an intake's)
reserves a spare that fits, and is minted under that spare's id, name, and account instead of
fresh ones, so a later change of default leaves it on the account it opened on, as any chat.
The spare stays hidden and leaves the pool, which is topped up. The chat's first send takes it
as a new chat would when the send's account and fast mode fit it; otherwise the spare is
destroyed and the chat created under the id once the spare is gone. One minted with no spare to
reserve (before any account is signed in, say), or whose reserved spare the session sweep
destroyed because its process died or it aged out, has the next spare the app starts on its
account's terms started under its own id and name, reserved for it ahead of the pool's, and takes
its account; a first send that comes before then creates the chat under the id. Such chats hold
at most as many reserved spares as the pool keeps; one past that holds none until a reservation
frees. A discarded chat returns its spare to the pool, and the session sweep destroys the oldest
ready spare beyond the pool's size.

A spare is created with the label ``chat_spare=true``, which is what every reader goes by: the
chat listings (a secondary chat's included) hide an agent so labelled, the launch wrapper starts
it in the most expendable memory band, and the memory report leaves it out. A hand-over lists the
chat at once and sets the label to false in the background. Nothing else is kept on disk: a
restart of the chat app destroys every agent still labelled a spare, except one whose chat
folder shows a chat already took it, which is relabelled instead. A reservation writes nothing
there, since the chat awaiting its first send is in memory only too, so a restart destroys a
reserved spare.
"""

from collections.abc import Sequence
from enum import auto
from typing import Final

from pydantic import Field

from imbue.chat.primitives import ChatId
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure

CHAT_SPARE_LABEL: Final[str] = "chat_spare"


class SpareChatPhase(UpperCaseStrEnum):
    """Where a spare agent is in its life."""

    # Its ``mngr create`` is running, or its harness has not said it accepts input yet.
    CREATING = auto()
    # Its harness accepts input; waiting for a new chat to take it.
    READY = auto()
    # Given to a chat awaiting its first send, whose id and name it carries: out of the pool, and
    # taken by that send, booted or not (``ready_at`` says which). The session sweep destroys a
    # booted one whose process died or that aged out.
    RESERVED = auto()
    # Taken by a new chat while still being created: it becomes that chat once its harness is up.
    CLAIMED = auto()
    # Reserved while still being created for a chat whose first send came on other terms: destroyed
    # as soon as its create ends, so that chat's own create can take the id.
    RELEASED = auto()
    # No longer wanted (its terms went stale, it aged out, its process died, its create failed, the
    # chat it was reserved for launched on other terms, or an earlier run of the app left it); its
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
    ready_at: float | None = Field(
        default=None, description="``time.monotonic()`` when its harness came up; None until it is ready"
    )

    def with_phase(self, phase: SpareChatPhase) -> "SpareChatAgent":
        return self.model_copy_update(to_update(self.field_ref().phase, phase))

    def as_ready(self, ready_at: float) -> "SpareChatAgent":
        """The spare once its harness is up: ready, or still reserved for the chat it was given to."""
        phase = SpareChatPhase.RESERVED if self.phase is SpareChatPhase.RESERVED else SpareChatPhase.READY
        return self.model_copy_update(
            to_update(self.field_ref().phase, phase), to_update(self.field_ref().ready_at, ready_at)
        )

    def is_starting(self) -> bool:
        """Whether its creation thread is still running: its ``mngr create``, or the wait for its harness."""
        if self.phase is SpareChatPhase.RESERVED:
            return self.ready_at is None
        return self.phase in (SpareChatPhase.CREATING, SpareChatPhase.CLAIMED, SpareChatPhase.RELEASED)

    def as_unreserved(self) -> "SpareChatAgent":
        """A reserved spare back in the pool: ready when its harness is up, else still being created."""
        return self.with_phase(SpareChatPhase.CREATING if self.ready_at is None else SpareChatPhase.READY)


@pure
def pooled_spares(spares: Sequence[SpareChatAgent]) -> tuple[SpareChatAgent, ...]:
    """The spares that count toward the pool: being created or waiting to be taken."""
    return tuple(spare for spare in spares if spare.phase in (SpareChatPhase.CREATING, SpareChatPhase.READY))
