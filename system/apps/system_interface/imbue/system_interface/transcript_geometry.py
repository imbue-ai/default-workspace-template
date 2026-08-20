"""What a client measured a transcript's rows to be, kept for the next visit.

The chat transcript reserves scroll space for history it has not loaded yet,
and the only honest source for how much space a row takes is a client that has
actually rendered and measured it: a row is a whole turn, and a turn is
anywhere from one event to hundreds, so no number this server could derive
would be right. Hence a store rather than a calculation -- nothing here reads
the numbers, it files what one client measured and hands it back to the next.

Measurements are keyed by the transcript's agent id **and** by a viewport width
bucket, because a row's height is a function of the width it wrapped at: the
same turn is one height in a narrow panel and another in a wide one, so
geometry taken at one width is not an answer at another. The bucket is the
client's own quantization of its viewport; this end only files under it.

Rows are kept or dropped one at a time. A row that does not describe a measured
range is skipped rather than failing the write, because this data outlives the
code that wrote it -- an older client, a hand-edited file -- and geometry a
client cannot use it simply measures again as it renders.

Storage mirrors ``member_last_used``: a small JSON file
(``transcript_geometry.json``) beside ``member_titles.json`` and
``member_last_used.json`` under the workspace layout dir, written under a
module-level lock. The file is created on the first write; a workspace nobody
has scrolled simply has none. It is a cache rather than a record, so it is
bounded on every axis it can grow along (see the caps below) -- a workspace
that has shown thousands of transcripts keeps only the few being read.

An agent's entry is dropped when the agent is destroyed (see
``clear_geometry``), since its transcript can never be rendered again and its
measurements would only hold a slot against a transcript someone is still
reading.
"""

import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated
from typing import Final
from typing import Self

from loguru import logger as _loguru_logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

_GEOMETRY_FILENAME: Final[str] = "transcript_geometry.json"

# How many transcripts keep geometry at once, least recently updated evicted
# first. Geometry is a measurement cache: an evicted transcript is measured
# again the next time it is opened, at the cost of one settling frame, so the
# file holds the handful being read rather than every transcript this workspace
# has ever shown.
_MAX_STORED_AGENTS: Final[int] = 50

# How many width buckets one transcript keeps. A bucket is a quantized viewport
# width, so a few cover every window a client actually uses; this is what stops
# a client reporting unusual widths from growing one transcript's entry without
# limit.
_MAX_STORED_WIDTH_BUCKETS_PER_AGENT: Final[int] = 8

# How many rows one entry keeps. The whole file is read and rewritten on every
# write, so this is what keeps that cost flat no matter how long a conversation
# runs.
_MAX_STORED_ROWS_PER_ENTRY: Final[int] = 5000

# Serializes every read-modify-write of the geometry file across the threaded
# WSGI server, exactly as ``member_last_used._last_used_lock`` does for the
# recency map.
_geometry_lock = threading.Lock()


class TranscriptGeometryKeyError(ValueError):
    """Raised when geometry is filed under a key nothing could have measured."""

    ...


class TranscriptGeometryRowError(ValueError):
    """Raised when a row's offsets do not describe a range of the transcript.

    Only ever raised from the row model's own validation, so callers meet it as
    the pydantic error that wraps it and skip the row rather than catching this.
    """

    ...


class TranscriptRowGeometry(FrozenModel):
    """One rendered row of a transcript, as some client measured it."""

    # Validated strictly, unlike most models here: the input is JSON a client
    # or a hand edit produced, and the ordinary lax rules would quietly turn
    # ``true`` into a one-pixel row and ``"0"`` into an offset. Neither is
    # something anything measured, and both would be stored as if they were.
    model_config = ConfigDict(strict=True)

    row_key: Annotated[str, Field(min_length=1)] = Field(description="The client's own identifier for the row")
    start_offset: Annotated[int, Field(ge=0)] = Field(description="Global index of the first event the row covers")
    end_offset: Annotated[int, Field(gt=0)] = Field(description="Global index one past the last event the row covers")
    height: Annotated[float, Field(gt=0)] = Field(description="Measured height of the rendered row, in CSS pixels")

    @model_validator(mode="after")
    def _reject_offsets_that_are_not_a_range(self) -> Self:
        if self.end_offset <= self.start_offset:
            raise TranscriptGeometryRowError(
                f"Row covers no events (start {self.start_offset}, end {self.end_offset})"
            )
        return self


class _StoredTranscriptGeometry(FrozenModel):
    """One transcript's measured rows at one viewport width, and when they landed."""

    rows: tuple[TranscriptRowGeometry, ...] = Field(description="Rows exactly as the client measured them")
    updated_at_ms: Annotated[int, Field(gt=0)] = Field(description="When this entry was last written, epoch ms")


class _StoredEntryShape(FrozenModel):
    """The shape a stored entry has to have before its rows are looked at.

    Separate from the entry itself so that the rows can be kept or dropped one
    at a time: reading them as a block would cost a transcript everything it
    measured over a single row an older client wrote differently.
    """

    model_config = ConfigDict(strict=True)

    # A list rather than a tuple because strict validation takes JSON's arrays
    # as what they parse to, and nothing reads this beyond handing it on.
    rows: list[object] = Field(description="Rows as the file holds them, before any of them is read")
    updated_at_ms: Annotated[int, Field(gt=0)] = Field(description="When this entry was last written, epoch ms")


def _now_ms() -> int:
    """The machine's clock, as epoch milliseconds."""
    return int(time.time() * 1000)


@pure
def _validated_agent_id(agent_id: str) -> str:
    """Whitespace-trim the transcript's agent id, rejecting one that is empty.

    The id is opaque here beyond being non-blank, exactly as a member ref is to
    the stores next door. A blank one names no transcript, and filing under it
    would hand one caller's measurements to every other blank-keyed caller.
    """
    trimmed = agent_id.strip()
    if not trimmed:
        raise TranscriptGeometryKeyError("Transcript geometry agent id is empty")
    return trimmed


@pure
def _validated_width_bucket_key(width_bucket: int) -> str:
    """The stored key for a viewport width bucket, rejecting a non-positive one.

    Buckets are stored as strings because JSON object keys are strings. A
    bucket at or below zero is no viewport at all, so nothing can have been
    measured at it.
    """
    if width_bucket <= 0:
        raise TranscriptGeometryKeyError(f"Transcript geometry width bucket {width_bucket} is not a viewport width")
    return str(width_bucket)


def _validated_rows(raw_rows: Sequence[object]) -> tuple[TranscriptRowGeometry, ...]:
    """The rows that are measurements, in the order given, up to the row cap.

    Rows are validated one at a time so that a single unusable row -- from an
    older client, or a hand-edited file -- costs only itself rather than the
    whole transcript's geometry.

    Past the cap it is the *trailing* rows that are kept. Nothing here reads the
    offsets, but the client sends its rows in transcript order and a transcript
    opens at its tail, so the end of that sequence is the part a reader arrives
    on. Keeping the front would drop geometry for exactly the turns the next
    visit renders first and hold onto history far above them.
    """
    validated_rows: list[TranscriptRowGeometry] = []
    skipped_row_count = 0
    for raw_row in raw_rows:
        try:
            validated_rows.append(TranscriptRowGeometry.model_validate(raw_row))
        except ValidationError:
            skipped_row_count += 1
    if skipped_row_count > 0:
        _loguru_logger.warning("Skipped {} transcript geometry rows that are not measurements", skipped_row_count)
    return tuple(validated_rows[-_MAX_STORED_ROWS_PER_ENTRY:])


@pure
def _keys_over_cap(recency_ms_by_key: dict[str, int], cap: int) -> tuple[str, ...]:
    """The keys to evict, least recently updated first, or () when under the cap."""
    if len(recency_ms_by_key) <= cap:
        return ()
    keys_by_recency = sorted(recency_ms_by_key.items(), key=lambda item: item[1])
    return tuple(key for key, _ in keys_by_recency[: len(recency_ms_by_key) - cap])


@pure
def _capped_buckets(
    geometry_by_bucket_key: dict[str, _StoredTranscriptGeometry],
) -> dict[str, _StoredTranscriptGeometry]:
    evicted_bucket_keys = _keys_over_cap(
        {bucket_key: entry.updated_at_ms for bucket_key, entry in geometry_by_bucket_key.items()},
        _MAX_STORED_WIDTH_BUCKETS_PER_AGENT,
    )
    return {
        bucket_key: entry
        for bucket_key, entry in geometry_by_bucket_key.items()
        if bucket_key not in evicted_bucket_keys
    }


@pure
def _capped_agents(
    geometry_by_agent_id: dict[str, dict[str, _StoredTranscriptGeometry]],
) -> dict[str, dict[str, _StoredTranscriptGeometry]]:
    """The stored transcripts, dropping the least recently updated over the cap.

    A transcript's recency is the most recent write across all of its width
    buckets, so measuring one that is being read at a second width keeps the
    first width's rows alive too.
    """
    evicted_agent_ids = _keys_over_cap(
        {
            agent_id: max(entry.updated_at_ms for entry in buckets.values())
            for agent_id, buckets in geometry_by_agent_id.items()
            if buckets
        },
        _MAX_STORED_AGENTS,
    )
    return {
        agent_id: buckets
        for agent_id, buckets in geometry_by_agent_id.items()
        if buckets and agent_id not in evicted_agent_ids
    }


def _geometry_path(layout_dir: Path) -> Path:
    return layout_dir / _GEOMETRY_FILENAME


def _parsed_entry(raw_entry: object) -> _StoredTranscriptGeometry | None:
    """One stored entry, or None when it is not one this store wrote.

    Reported by the caller, which is the loop that knows how many entries a read
    lost in total.
    """
    try:
        stored_shape = _StoredEntryShape.model_validate(raw_entry)
    except ValidationError:
        return None
    return _StoredTranscriptGeometry(
        rows=_validated_rows(stored_shape.rows),
        updated_at_ms=stored_shape.updated_at_ms,
    )


def _read_unlocked(layout_dir: Path) -> dict[str, dict[str, _StoredTranscriptGeometry]]:
    """The stored geometry, tolerating an absent, corrupt or hand-edited file.

    A file that cannot be read is reported as empty (logged at warning) rather
    than crashing the transcript: every row is measurable again as it renders,
    which is exactly what a client does before anything was ever stored.
    """
    geometry_path = _geometry_path(layout_dir)
    if not geometry_path.exists():
        return {}
    try:
        stored = json.loads(geometry_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _loguru_logger.opt(exception=e).warning(
            "Failed to read {}; treating every transcript as unmeasured", geometry_path
        )
        return {}
    geometry_by_agent_id: object = stored.get("geometry_by_agent_id") if isinstance(stored, dict) else None
    if not isinstance(geometry_by_agent_id, dict):
        return {}
    parsed_by_agent_id: dict[str, dict[str, _StoredTranscriptGeometry]] = {}
    # Counted across the whole read and reported once, as _validated_rows reports
    # the rows it skips within one entry. Losing a whole entry is the larger of
    # the two events, so it must not be the quieter one -- a drift in the stored
    # shape would otherwise send every affected transcript back to measuring from
    # scratch with nothing to point at.
    dropped_entry_count = 0
    for agent_id, raw_buckets in geometry_by_agent_id.items():
        if not isinstance(agent_id, str) or not isinstance(raw_buckets, dict):
            continue
        parsed_buckets: dict[str, _StoredTranscriptGeometry] = {}
        for bucket_key, raw_entry in raw_buckets.items():
            parsed_entry = _parsed_entry(raw_entry) if isinstance(bucket_key, str) else None
            if parsed_entry is None:
                dropped_entry_count += 1
                continue
            parsed_buckets[bucket_key] = parsed_entry
        if parsed_buckets:
            parsed_by_agent_id[agent_id] = parsed_buckets
    if dropped_entry_count > 0:
        _loguru_logger.warning(
            "Dropped {} transcript geometry entries in {} that this store did not write",
            dropped_entry_count,
            geometry_path,
        )
    return parsed_by_agent_id


def _write_unlocked(layout_dir: Path, geometry_by_agent_id: dict[str, dict[str, _StoredTranscriptGeometry]]) -> None:
    layout_dir.mkdir(parents=True, exist_ok=True)
    serializable_by_agent_id = {
        agent_id: {bucket_key: entry.model_dump() for bucket_key, entry in buckets.items()}
        for agent_id, buckets in geometry_by_agent_id.items()
    }
    # Written compact rather than indented like the stores next door: this file
    # holds thousands of rows rather than a handful of names, and it is machine
    # state nobody reads by hand.
    _geometry_path(layout_dir).write_text(json.dumps({"geometry_by_agent_id": serializable_by_agent_id}))


def read_geometry(layout_dir: Path, agent_id: str, width_bucket: int) -> tuple[TranscriptRowGeometry, ...]:
    """What this workspace last measured for one transcript at one width.

    No rows is the ordinary answer for a transcript nobody has measured at this
    width yet: the caller measures as it renders rather than being handed a
    guess this end would have had to invent.
    """
    stored_agent_id = _validated_agent_id(agent_id)
    bucket_key = _validated_width_bucket_key(width_bucket)
    with _geometry_lock:
        entry = _read_unlocked(layout_dir).get(stored_agent_id, {}).get(bucket_key)
    if entry is None:
        return ()
    return entry.rows


def _drop_bucket(layout_dir: Path, stored_agent_id: str, bucket_key: str) -> None:
    """Forget one transcript's geometry at one width, writing only if it had any.

    Staying silent when there was nothing to remove is what keeps a workspace
    nobody has measured from acquiring a geometry file on the strength of a
    write that measured nothing.
    """
    with _geometry_lock:
        geometry_by_agent_id = _read_unlocked(layout_dir)
        buckets = geometry_by_agent_id.get(stored_agent_id)
        if buckets is None or bucket_key not in buckets:
            return
        del buckets[bucket_key]
        if not buckets:
            del geometry_by_agent_id[stored_agent_id]
        _write_unlocked(layout_dir, geometry_by_agent_id)


def write_geometry(
    layout_dir: Path,
    agent_id: str,
    width_bucket: int,
    rows: Sequence[object],
) -> tuple[TranscriptRowGeometry, ...]:
    """File what a client measured for one transcript at one width, and hand it back.

    The rows replace whatever was stored for that transcript at that width: the
    client that just rendered it measured the whole of what it rendered, and
    merging two clients' measurements would mean deciding which is right, which
    is precisely the judgement this store does not make. What comes back is
    what was actually kept, so a caller sees the rows that were dropped as
    unusable without this end having to explain them.

    A write naming no usable row stores nothing and drops whatever was held at
    that width, because that is what replacement means when the replacement is
    empty. Keeping it would file an entry that reads back the same as one that
    was never written while still holding a slot in both caps below, so a run of
    them would evict the transcripts someone is actually reading.
    """
    stored_agent_id = _validated_agent_id(agent_id)
    bucket_key = _validated_width_bucket_key(width_bucket)
    validated_rows = _validated_rows(rows)
    if not validated_rows:
        _drop_bucket(layout_dir, stored_agent_id, bucket_key)
        return ()
    entry = _StoredTranscriptGeometry(rows=validated_rows, updated_at_ms=_now_ms())
    with _geometry_lock:
        geometry_by_agent_id = _read_unlocked(layout_dir)
        # What was just written is re-filed at the end of both maps, so that
        # entries sharing a millisecond -- the clock is coarser than a burst of
        # writes from one client -- still evict oldest-first when the caps bite.
        buckets = dict(geometry_by_agent_id.pop(stored_agent_id, {}))
        buckets.pop(bucket_key, None)
        buckets[bucket_key] = entry
        geometry_by_agent_id[stored_agent_id] = _capped_buckets(buckets)
        _write_unlocked(layout_dir, _capped_agents(geometry_by_agent_id))
    return entry.rows


def clear_geometry(layout_dir: Path, agent_id: str) -> bool:
    """Drop a destroyed transcript's geometry, reporting whether it had any.

    Destroy is what this exists for, as it is for ``clear_last_used``: the
    transcript can never be rendered again, so every width bucket goes at once
    and stops holding a slot against a transcript someone is still reading. A
    transcript nobody measured returns False and writes nothing.
    """
    stored_agent_id = _validated_agent_id(agent_id)
    with _geometry_lock:
        geometry_by_agent_id = _read_unlocked(layout_dir)
        if stored_agent_id not in geometry_by_agent_id:
            return False
        del geometry_by_agent_id[stored_agent_id]
        _write_unlocked(layout_dir, geometry_by_agent_id)
        return True
