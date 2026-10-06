"""The compaction signals a chat reads: mngr's Claude hook files, its own request record, and the compacted event.

mngr's Claude ``PreCompact`` hook writes a ``compacting`` marker into the agent state dir and its
``PostCompact`` hook removes it and records ``last_compaction.json`` (both named in
``imbue.mngr_claude.claude_config``). A cancelled or failed compaction fires no ``PostCompact``,
so the marker can outlive its compaction.

The chat app records each compaction it asks for in ``compaction_request.json`` beside them, so
the cause survives the chat app's memory of the request.

Every harness's parser marks a finished compaction with the same event: a ``user_message`` whose
``display`` is ``status`` and whose content is "Context was compacted" (see
``harnesses/events.py``).
"""

import os
import tempfile
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.chat.activity_state import ActivityState
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

# The file (in the agent state dir) recording the last compaction the chat app asked for:
# ``{"cause": "idle"|"manual", "requested_at": "<utc iso>"}``.
COMPACTION_REQUEST_FILENAME: Final[str] = "compaction_request.json"


class CompactionTrigger(LowerCaseStrEnum):
    """The ``trigger`` mngr's compaction hooks record, from Claude Code's hook payload."""

    MANUAL = auto()
    AUTO = auto()
    UNKNOWN = auto()


class CompactionSignal(FrozenModel):
    """One of the hook files read back: what triggered the compaction and when the hook wrote it."""

    trigger: CompactionTrigger = Field(description="The recorded trigger; UNKNOWN when absent or unrecognized")
    written_at: float = Field(
        description="Epoch seconds of the recorded time, else the file's mtime; never later than the read"
    )


class CompactionRequest(FrozenModel):
    """The compaction the chat app last asked for, read back from ``compaction_request.json``."""

    cause: CompactionCause = Field(description="Why the chat asked: the idle sweep or a composer /compact")
    requested_at: float = Field(description="Epoch seconds of the request; never later than the read")


class _RawCompactionRecord(FrozenModel):
    """The JSON the hooks write; every field optional, since the file is read as found."""

    model_config = {"extra": "ignore"}

    trigger: str | None = Field(default=None, description="manual, auto, or unknown")
    started_at: str | None = Field(default=None, description="UTC ISO time the compaction started (the marker)")
    ended_at: str | None = Field(default=None, description="UTC ISO time the compaction ended (the record)")


class _RawCompactionRequest(FrozenModel):
    """The JSON ``write_compaction_request`` writes."""

    model_config = {"extra": "ignore"}

    cause: CompactionCause = Field(description="idle or manual")
    requested_at: str = Field(description="UTC ISO time of the request")


@pure
def _parse_trigger(raw_trigger: str | None) -> CompactionTrigger:
    if raw_trigger is None:
        return CompactionTrigger.UNKNOWN
    try:
        return CompactionTrigger(raw_trigger)
    except ValueError:
        return CompactionTrigger.UNKNOWN


def read_compaction_signal(path: Path, now: float) -> CompactionSignal | None:
    """Read the ``compacting`` marker or ``last_compaction.json`` at ``path``; None when it does not exist.

    An unreadable body or a missing time falls back to UNKNOWN and the file's mtime, so a marker
    always counts for as long as it is fresh. A recorded time later than ``now`` (clock skew)
    also falls back to the mtime, and an mtime later than ``now`` reads as ``now``, so a marker
    cannot stay fresh for longer than the timeout after it was written.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    try:
        content = path.read_text()
    except OSError:
        return None
    except UnicodeDecodeError as e:
        logger.warning("Failed to decode the compaction record at {}, using its mtime: {}", path, e)
        return CompactionSignal(trigger=CompactionTrigger.UNKNOWN, written_at=min(mtime, now))
    try:
        raw = _RawCompactionRecord.model_validate_json(content)
    except ValidationError as e:
        logger.warning("Failed to parse the compaction record at {}, using its mtime: {}", path, e)
        return CompactionSignal(trigger=CompactionTrigger.UNKNOWN, written_at=min(mtime, now))
    recorded_at = parse_iso_timestamp_to_epoch(raw.started_at or raw.ended_at)
    if recorded_at is not None and recorded_at <= now:
        written_at = recorded_at
    else:
        written_at = min(mtime, now)
    return CompactionSignal(trigger=_parse_trigger(raw.trigger), written_at=written_at)


def write_compaction_request(path: Path, cause: CompactionCause, requested_at: float) -> None:
    """Record a compaction the chat asked for at ``path``, replacing the previous one atomically.

    Never creates the directory: an agent with no local state dir has nothing local to read it.
    Raises OSError when the file cannot be written.
    """
    content = _RawCompactionRequest(
        cause=cause, requested_at=datetime.fromtimestamp(requested_at, tz=timezone.utc).isoformat()
    ).model_dump_json()
    file_descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def read_compaction_request(path: Path, now: float) -> CompactionRequest | None:
    """Read ``compaction_request.json`` at ``path``; None when it is absent or unreadable.

    A ``requested_at`` later than ``now`` (clock skew) reads as ``now``.
    """
    try:
        content = path.read_text()
    except OSError:
        return None
    except UnicodeDecodeError as e:
        logger.warning("Failed to decode the compaction request at {}: {}", path, e)
        return None
    try:
        raw = _RawCompactionRequest.model_validate_json(content)
    except ValidationError as e:
        logger.warning("Failed to parse the compaction request at {}: {}", path, e)
        return None
    requested_at = parse_iso_timestamp_to_epoch(raw.requested_at)
    if requested_at is None:
        logger.warning("Failed to parse the time of the compaction request at {}", path)
        return None
    return CompactionRequest(cause=raw.cause, requested_at=min(requested_at, now))


@pure
def shown_compaction_cause(
    marker_cause: CompactionCause | None, pending_cause: CompactionCause | None, derived_state: ActivityState
) -> CompactionCause | None:
    """The cause of the compaction a chat shows, or None when it shows its turn state instead.

    A live marker always shows (Claude's own auto-compaction runs mid-turn). The chat's own
    request shows only over an idle agent: a /compact queued behind a running turn waits for it.
    """
    if marker_cause is not None:
        return marker_cause
    if derived_state == ActivityState.IDLE:
        return pending_cause
    return None


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
