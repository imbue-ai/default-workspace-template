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

Event-id rule (the spine): every event's ``event_id`` MUST be derived from the harness's
own STABLE identity -- claude's message UUID, codex's message id / call_id / ``turn_id``,
pi's entry id -- never a physical line/counter position. A stable id
lets the store dedup a re-serialised copy, supersede an updated one in place (rather than
appending a duplicate), and re-materialise a rotated rollout without duplicating; a counter
gives a re-added entry a new id and makes those impossible. A harness synthesises an id from
content only where the source truly carries none, and does so position-independently.
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


class DisplayKind(StrEnum):
    """How the frontend must render one event -- the DECISION, not the evidence.

    A harness's own markers (claude's ``isMeta``, a tk lifecycle verb, a latchkey host, a
    framework sentinel) are read backend-side and become one of these; the raw markers never
    cross the wire. The frontend maps each kind to its visual (see ``KIND_SPEC`` in
    ``message-kinds.ts``) with zero sniffing of harness data.

    Carried in the optional ``display`` field of ``user_message`` (with ``display_label`` /
    ``display_body`` for the chip title / unwrapped body) and ``tool_call`` (``HIDDEN`` and
    ``PERMISSION_REQUEST`` only). Absent = render normally. A sibling of
    :class:`SpecialEventKind`, deliberately not the same enum: ``special`` says an event is
    not a message at all (and renderers ignore it), while ``display`` says how a message
    renders -- a hidden message is still a message and still occupies its ``/events`` slot.
    """

    # No DOM at all (the seeded /welcome, a model-bar command, a framework-injected line).
    HIDDEN = "hidden"
    # A collapsed chip inside the current turn; ``display_label`` is its title.
    CHIP = "chip"
    # Relocated into the preceding Skill tool-call block; ``display_label`` is the skill name.
    SKILL_EXPANSION = "skill_expansion"
    # tool_call only: render the rich permission card instead of a tool row.
    PERMISSION_REQUEST = "permission_request"
    # user_message only: a latchkey verdict -- no row; the ``resolution`` field is written
    # onto the earlier permission card.
    PERMISSION_RESOLUTION = "permission_resolution"
