"""The transcript event contract every harness fills.

A harness's watcher and parser turn its own transcript into the events the frontend
renders. Three core types are emitted by every harness with the same fields, so no view
needs to know which harness produced them:

    user_message        a user (or framework-injected) turn
    assistant_message   assistant text plus its tool calls
    tool_result         one tool call's output

Some harnesses record markers that are not messages -- codex writes explicit turn
boundaries to its rollout, claude's transcript has none. Those arrive as a fourth type,
``special``, carrying a ``kind`` drawn from :class:`SpecialEventKind`. Renderers ignore
them; they exist so ``/events`` reflects the true transcript and so activity derivation
has an authoritative signal.

``SpecialEventKind`` is the ONLY list of legal kinds, and each harness declares the subset
it may emit via ``HarnessSpec.special_kinds``. A harness emitting a kind outside its own
declaration is a bug its tests should catch. The frontend mirrors this enum, so an
undeclared kind is a type error there rather than an event silently dropped on the floor.
"""

from enum import StrEnum
from typing import Final

# The `type` field of the fourth event kind. The three core types are string literals in
# each parser, matching the frontend's discriminated union.
SPECIAL_EVENT_TYPE: Final[str] = "special"

# How far a tool call's input preview and a tool result's output are clipped for the
# wire, shared by every harness's parser. Defined once here (not per-parser) because
# they are part of the event contract and MUST agree across harnesses: e.g. codex's
# tool_labels reasons about "the 200-char preview", so a drift between the two parsers
# would silently break its labels. (A backend cap the frontend relies on, so widening
# or exempting a tool output is a change to this one value / its call sites.)
MAX_TOOL_INPUT_PREVIEW_LENGTH: Final[int] = 200
MAX_TOOL_OUTPUT_LENGTH: Final[int] = 2000


class SpecialEventKind(StrEnum):
    """Every non-message marker any harness may emit.

    Turn boundaries come from codex, which records them in real time: ``task_started``
    when a user turn begins, ``task_complete`` when it ends, and ``turn_aborted`` on a
    user interrupt. Claude's transcript carries no equivalent, so claude declares an
    empty set -- that emptiness is the honest statement, not an omission.
    """

    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_ABORTED = "turn_aborted"
