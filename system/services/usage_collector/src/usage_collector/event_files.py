"""Following every usage event file in the workspace: the source of truth the store is built from.

Every emitter appends model_call events to a file, and the collector only reads
files, about once a second, each from where it left off (its place is kept in
the store, so a restart picks up there):

- each agent's mngr_usage event file
  (``<host dir>/agents/<agent>/events/<source>/usage/events.jsonl``), and the
  copies mngr preserves for destroyed agents under ``<host dir>/preserved/``;
- the files workspace_usage writes for apps and one-shot calls
  (``data/.apps/usage/events/*.jsonl``).

The store is an index of these files: deleting it rebuilds it from them, and
deduplicating by event id makes the re-read safe. Only lines that are
model_call events (they carry a ``schema_version`` and an ``attribution``) are
ingested as calls; the older snapshot events in agents' files are read only for
the subscription limit readings they carry (see ``limits``).
Each is attributed to this workspace, since an agent cannot tell which
workspace it runs in and every file here is this workspace's. A file that
shrank or was replaced is read again from the start.
"""

import json
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Final

from imbue.imbue_common.frozen_model import FrozenModel
from loguru import logger
from pydantic import Field

from usage_collector.store import UsageStore

AGENT_EVENT_FILE_GLOBS: Final[tuple[str, ...]] = (
    "agents/*/events/*/usage/events.jsonl",
    "preserved/*/events/*/usage/events.jsonl",
)
WORKSPACE_EVENT_FILE_GLOB: Final[str] = "*.jsonl"
# Bounds one pass over a file that grew a lot while the collector was down.
_MAX_BYTES_PER_PASS: Final[int] = 8 * 1024 * 1024


def is_model_call_line(record: object) -> bool:
    return (
        isinstance(record, dict)
        and "schema_version" in record
        and "attribution" in record
    )


def event_files(host_dir: Path | None, workspace_events_dir: Path) -> Iterator[Path]:
    """Every file to follow: the agents' on ``host_dir`` (when there is one), then workspace_usage's."""
    if host_dir is not None:
        for pattern in AGENT_EVENT_FILE_GLOBS:
            yield from sorted(host_dir.glob(pattern))
    yield from sorted(workspace_events_dir.glob(WORKSPACE_EVENT_FILE_GLOB))


def with_workspace_id(record: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    """``record`` attributed to ``workspace_id``."""
    attribution = record.get("attribution")
    if not isinstance(attribution, dict):
        return record
    return {**record, "attribution": {**attribution, "workspace_id": workspace_id}}


class PassResult(FrozenModel):
    """What one pass over the event files read."""

    events_read: int = Field(description="model_call lines read and handed to ingest")
    is_complete: bool = Field(
        description="Whether every file was read to its end, rather than stopped at the per-pass cap"
    )


def follow_event_files(
    paths: Iterable[Path],
    store: UsageStore,
    ingest: Callable[[object], object],
    workspace_id: str,
    observe_limits: Callable[[object], object] | None = None,
) -> PassResult:
    """Ingest the model_call lines added to each file since the last pass, and hand every line to ``observe_limits``.

    The agents' older snapshot events are not model_calls, but they are where a subscription's limit readings are.
    """
    read = 0
    is_complete = True
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        inode, offset = store.file_position(path)
        if inode != stat.st_ino or stat.st_size < offset:
            offset = 0
        if stat.st_size == offset:
            continue
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read(_MAX_BYTES_PER_PASS)
            if offset + len(chunk) < stat.st_size:
                is_complete = False
        except OSError as e:
            logger.warning("Could not read agent usage file {}: {}", path, e)
            continue
        # Only whole lines: a writer may be partway through the last one.
        complete = chunk[: chunk.rfind(b"\n") + 1]
        for line in complete.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Skipped an unreadable line in {}: {}", path, e)
                continue
            if observe_limits is not None:
                observe_limits(record)
            if is_model_call_line(record):
                ingest(with_workspace_id(record, workspace_id))
                read += 1
        store.set_file_position(path, stat.st_ino, offset + len(complete))
    return PassResult(events_read=read, is_complete=is_complete)
