"""The compaction signals a chat reads beside its own: mngr's Claude hook files and the compacted event.

mngr's Claude ``PreCompact`` hook writes a ``compacting`` marker into the agent state dir and its
``PostCompact`` hook removes it and records ``last_compaction.json`` (both named in
``imbue.mngr_claude.claude_config``). A cancelled or failed compaction fires no ``PostCompact``,
so the marker can outlive its compaction; the manager ignores one older than its
``PENDING_COMPACTION_TIMEOUT_SECONDS`` and removes it when the user interrupts.

Every harness's parser marks a finished compaction with the same event: a ``user_message`` whose
``display`` is ``status`` and whose content is "Context was compacted" (see
``harnesses/events.py``). The manager attaches ``compaction_cause`` to it.
"""

from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.chat.activity_state import CompactionCause
from imbue.chat.activity_state import parse_iso_timestamp_to_epoch
from imbue.chat.harnesses.events import DisplayKind
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# The content every harness's parser gives its compacted event.
CONTEXT_COMPACTED_CONTENT: Final[str] = "Context was compacted"

# The event field the manager stamps with the cause, read by the pill's "why?" popover.
COMPACTION_CAUSE_FIELD: Final[str] = "compaction_cause"


class CompactionTrigger(LowerCaseStrEnum):
    """The ``trigger`` mngr's compaction hooks record, from Claude Code's hook payload."""

    MANUAL = auto()
    AUTO = auto()
    UNKNOWN = auto()


class CompactionSignal(FrozenModel):
    """One of the hook files read back: what triggered the compaction and when the hook wrote it."""

    trigger: CompactionTrigger = Field(description="The recorded trigger; UNKNOWN when absent or unrecognized")
    written_at: float = Field(description="Epoch seconds of the recorded time, else the file's mtime")


class _RawCompactionRecord(FrozenModel):
    """The JSON the hooks write; every field optional, since the file is read as found."""

    model_config = {"extra": "ignore"}

    trigger: str | None = Field(default=None, description="manual, auto, or unknown")
    started_at: str | None = Field(default=None, description="UTC ISO time the compaction started (the marker)")
    ended_at: str | None = Field(default=None, description="UTC ISO time the compaction ended (the record)")


@pure
def _parse_trigger(raw_trigger: str | None) -> CompactionTrigger:
    if raw_trigger is None:
        return CompactionTrigger.UNKNOWN
    try:
        return CompactionTrigger(raw_trigger)
    except ValueError:
        return CompactionTrigger.UNKNOWN


def read_compaction_signal(path: Path) -> CompactionSignal | None:
    """Read the ``compacting`` marker or ``last_compaction.json`` at ``path``; None when it does not exist.

    An unreadable body or a missing time falls back to UNKNOWN and the file's mtime, so a marker
    always counts for as long as it is fresh.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    try:
        content = path.read_text()
    except OSError:
        return None
    try:
        raw = _RawCompactionRecord.model_validate_json(content)
    except ValidationError as e:
        logger.warning("Failed to parse the compaction record at {}, using its mtime: {}", path, e)
        return CompactionSignal(trigger=CompactionTrigger.UNKNOWN, written_at=mtime)
    recorded_at = parse_iso_timestamp_to_epoch(raw.started_at or raw.ended_at)
    return CompactionSignal(
        trigger=_parse_trigger(raw.trigger),
        written_at=mtime if recorded_at is None else recorded_at,
    )


@pure
def cause_of_compacting_marker(trigger: CompactionTrigger, pending_cause: CompactionCause | None) -> CompactionCause:
    """The cause behind a live marker: Claude's own auto-compaction, else what the chat asked for, else the user."""
    match trigger:
        case CompactionTrigger.AUTO:
            return CompactionCause.NATIVE
        case CompactionTrigger.MANUAL | CompactionTrigger.UNKNOWN:
            return CompactionCause.MANUAL if pending_cause is None else pending_cause
        case _ as unreachable:
            assert_never(unreachable)


@pure
def cause_of_last_compaction(trigger: CompactionTrigger) -> CompactionCause | None:
    """The cause a ``last_compaction.json`` alone can name; None when it does not say."""
    match trigger:
        case CompactionTrigger.AUTO:
            return CompactionCause.NATIVE
        case CompactionTrigger.MANUAL:
            return CompactionCause.MANUAL
        case CompactionTrigger.UNKNOWN:
            return None
        case _ as unreachable:
            assert_never(unreachable)


@pure
def is_context_compacted_event(event: dict[str, Any]) -> bool:
    """Whether a transcript event is a harness's "Context was compacted" status event."""
    return (
        event.get("type") == "user_message"
        and event.get("display") == DisplayKind.STATUS.value
        and event.get("content") == CONTEXT_COMPACTED_CONTENT
    )
