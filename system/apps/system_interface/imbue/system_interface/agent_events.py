"""How this system interface obtains agent lifecycle events, and whether they arrive.

The mechanics of reading another process's event stream belong to mngr, which owns
the file format: :class:`imbue.mngr.api.observe.ObserveEventFollower` tails it,
:func:`imbue.mngr.api.observe.is_observe_writer_running` says whether anyone is
writing it. What stays here is the part specific to *this* app -- which of the two
sources an instance uses, and how it reports whether events are actually reaching
it.

That choice exists because ``mngr observe`` is single-writer per host dir (it holds
an exclusive ``flock`` for its whole run). A system interface serving the workspace
owns the observer (:attr:`AgentEventsMode.OBSERVE`) and consumes its
``--stream-events`` stdout. A *second* one on the same host -- the live-editing
preview, or the reveal script's pre-flight boot -- must not try to start its own:
the lock would reject it, the observer would exit seconds into boot, and that
instance's agent view would silently freeze forever while every other part of it
kept working. Such an instance runs in :attr:`AgentEventsMode.FOLLOW` instead.

:class:`AgentEventsStatus` is what the ``/api/health`` endpoint reports. It exists
because "can I list agents" is not the same question: a one-shot discovery succeeds
just as well on an instance whose lifecycle stream is dead, which is exactly how a
broken preview used to pass its boot health check.
"""

from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class AgentEventsMode(UpperCaseStrEnum):
    """How this system interface obtains agent lifecycle events."""

    # Run ``mngr observe --stream-events`` and consume its stdout. Requires the
    # observe lock, so at most one instance per host may do this.
    OBSERVE = auto()
    # Read the event file that another process's observer is writing. Takes no
    # lock, so any number of instances may do this alongside the one observer.
    FOLLOW = auto()


class AgentEventsStatus(FrozenModel):
    """Whether the agent-lifecycle event stream is actually feeding this instance.

    ``is_stream_healthy`` is the thing a health gate must assert. It is deliberately *not*
    "can I list agents": a one-shot discovery works fine on an instance whose
    lifecycle stream is dead, which is exactly how a broken preview used to pass
    its health check.
    """

    mode: AgentEventsMode = Field(description="How this instance sources lifecycle events")
    is_stream_healthy: bool = Field(description="Whether lifecycle events are actually reaching this instance")
    detail: str = Field(description="Human-readable explanation of the current state")
