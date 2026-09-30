"""The app announcements: how the minds desktop learns which origins a workspace's apps expose.

The stream keeps mngr's wire vocabulary (``events/services/events.jsonl``, ``service_registered``,
``service_deregistered``, a ``service`` field); in the shell's own code an app is an app.

Every read of the app registry is announced to ``$MNGR_AGENT_STATE_DIR/events/services/events.jsonl`` as one
``service_registered`` event per app whose registered fields (URL, label, icon) differ from the last announced,
and one ``service_deregistered`` per app that left. Only changed rows are announced: ``forward_port.py`` rewrites
the whole registry whenever any app registers, so a write says nothing about which apps moved, and an app
restarting in a loop would otherwise re-announce every app in the file on every restart. The first read after
the shell starts remembers nothing and announces every app, which is what a consumer reading the stream from its
start needs. A stream over the archive threshold at that first read is moved aside before it and gzipped off-thread
after, to a name mngr does not replay. The stream is plumbing the shell writes; it imports nothing from mngr.
"""

import gzip
import os
import shutil
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

from app_manifest.registry import RegistryRow
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.event_envelope import EventEnvelope
from imbue.imbue_common.event_envelope import EventId
from imbue.imbue_common.event_envelope import EventSource
from imbue.imbue_common.event_envelope import EventType
from imbue.imbue_common.event_envelope import IsoTimestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import generate_rotation_timestamp
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# The agent state directory mngr hands every process of the workspace; the stream lives under it.
ENV_AGENT_STATE_DIR: Final[str] = "MNGR_AGENT_STATE_DIR"
ANNOUNCEMENTS_REL: Final[str] = "events/services/events.jsonl"

ANNOUNCEMENT_SOURCE: Final[EventSource] = EventSource("services")
REGISTERED_ANNOUNCEMENT_TYPE: Final[EventType] = EventType("service_registered")
DEREGISTERED_ANNOUNCEMENT_TYPE: Final[EventType] = EventType("service_deregistered")

# Consumers replay the whole stream each time they attach, and the retired app-watcher service, which re-announced
# every app on each registry rewrite, grew some streams past a gigabyte.
ANNOUNCEMENTS_ARCHIVE_THRESHOLD_BYTES: Final[int] = 5 * 1024 * 1024
_KEPT_ANNOUNCEMENTS_ARCHIVE_COUNT: Final[int] = 3
# Neither suffix fits mngr's replayed rotation name (``events.jsonl.<digits>``).
_STAGED_ANNOUNCEMENTS_SUFFIX: Final[str] = ".archiving"
_ANNOUNCEMENTS_ARCHIVE_SUFFIX: Final[str] = ".gz"


class AppRegisteredAnnouncement(EventEnvelope):
    """An app registered its URL with the agent (the ``service_registered`` event minds reads)."""

    app: str = Field(serialization_alias="service", description="The registered app name")
    url: str = Field(description="Where the app is reachable from inside the workspace")
    label: str = Field(
        default="",
        description="The app's unguessable origin label; empty for a legacy row written before labels existed",
    )
    icon: str = Field(default="", description="The app's registered SVG icon markup, verbatim; empty when none")


class AppDeregisteredAnnouncement(EventEnvelope):
    """An app that was registered is no longer available (the ``service_deregistered`` event minds reads)."""

    app: str = Field(serialization_alias="service", description="The app name that left the registry")


class AnnouncedRow(FrozenModel):
    """One app's registered fields: everything a registration event carries but its name."""

    url: str = Field(description="The registered URL")
    label: str = Field(description="The origin label")
    icon: str = Field(description="The icon markup, empty when none")


class AnnouncementDiff(FrozenModel):
    """What one registry read changes against the last announcement, in emission order."""

    changed: tuple[str, ...] = Field(description="The names whose row is new or differs, in registry order")
    gone: tuple[str, ...] = Field(description="The names that left, sorted")


@pure
def announced_row_of(row: RegistryRow) -> AnnouncedRow:
    return AnnouncedRow(url=str(row.url), label=row.label, icon=row.icon or "")


@pure
def announced_rows_of(rows: Sequence[RegistryRow]) -> dict[str, AnnouncedRow]:
    return {str(row.name): announced_row_of(row) for row in rows}


@pure
def diff_announced_rows(current: dict[str, AnnouncedRow], previous: dict[str, AnnouncedRow]) -> AnnouncementDiff:
    changed = tuple(name for name, row in current.items() if previous.get(name) != row)
    gone = tuple(sorted(set(previous) - set(current)))
    return AnnouncementDiff(changed=changed, gone=gone)


def announcements_path_from_environment() -> Path | None:
    """Where the stream lives, or None outside a workspace (the variable unset)."""
    state_dir = os.environ.get(ENV_AGENT_STATE_DIR)
    if not state_dir:
        return None
    return Path(state_dir) / ANNOUNCEMENTS_REL


def _staged_announcements(events_path: Path) -> list[Path]:
    return sorted(events_path.parent.glob(f"{events_path.name}.*{_STAGED_ANNOUNCEMENTS_SUFFIX}"))


def stage_oversized_announcements(events_path: Path) -> bool:
    """Move a stream over the archive threshold aside, so the next write starts a new one; answers whether it did.

    Only renames, so it is quick enough to run before a write; ``compress_staged_announcements`` compresses.
    """
    try:
        stream_size = events_path.stat().st_size
    except FileNotFoundError:
        return False
    if stream_size <= ANNOUNCEMENTS_ARCHIVE_THRESHOLD_BYTES:
        return False
    events_path.rename(
        events_path.with_name(f"{events_path.name}.{generate_rotation_timestamp()}{_STAGED_ANNOUNCEMENTS_SUFFIX}")
    )
    return True


def compress_staged_announcements(events_path: Path) -> None:
    """Gzip every stream staged beside ``events_path`` into its archive, then keep only the newest archives."""
    for staged_path in _staged_announcements(events_path):
        archive_path = staged_path.with_name(
            staged_path.name.removesuffix(_STAGED_ANNOUNCEMENTS_SUFFIX) + _ANNOUNCEMENTS_ARCHIVE_SUFFIX
        )
        partial_path = archive_path.with_name(archive_path.name + ".partial")
        # The fastest level keeps the one-off CPU burst short in a container shared with the user's agents.
        with open(staged_path, "rb") as source, gzip.open(partial_path, "wb", compresslevel=1) as target:
            shutil.copyfileobj(source, target)
        partial_path.rename(archive_path)
        staged_path.unlink()

    archive_paths = sorted(events_path.parent.glob(f"{events_path.name}.*{_ANNOUNCEMENTS_ARCHIVE_SUFFIX}"))
    for expired_path in archive_paths[:-_KEPT_ANNOUNCEMENTS_ARCHIVE_COUNT]:
        expired_path.unlink()


def _new_event_id() -> EventId:
    return EventId(f"evt-{uuid4().hex}")


def _now_iso() -> IsoTimestamp:
    # The envelope carries nanosecond precision; the clock gives microseconds, padded.
    return IsoTimestamp(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z")


class AppAnnouncementWriter(MutableModel):
    """Appends the registration events one registry read implies; remembers what it last announced."""

    model_config = {"extra": "forbid", "frozen": False}

    events_path: Path = Field(
        frozen=True, description="The stream's file, created with its directories on first write"
    )

    _announced: dict[str, AnnouncedRow] = PrivateAttr(default_factory=dict)
    _is_stream_size_checked: bool = PrivateAttr(default=False)
    # The inventory announces from more than one thread.
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def announce(self, rows: Sequence[RegistryRow]) -> None:
        """Write a registration per row whose fields changed and a deregistration per row that left, then remember
        the rows as announced, one read at a time. A write that fails leaves the memory untouched, so the next read
        announces again."""
        with self._lock:
            self._announce_locked(rows)

    def _announce_locked(self, rows: Sequence[RegistryRow]) -> None:
        if not self._is_stream_size_checked:
            self._is_stream_size_checked = True
            self._archive_oversized_stream()
        current = announced_rows_of(rows)
        diff = diff_announced_rows(current, self._announced)
        if not diff.changed and not diff.gone:
            return
        lines = [
            AppRegisteredAnnouncement(
                timestamp=_now_iso(),
                type=REGISTERED_ANNOUNCEMENT_TYPE,
                event_id=_new_event_id(),
                source=ANNOUNCEMENT_SOURCE,
                app=name,
                url=current[name].url,
                label=current[name].label,
                icon=current[name].icon,
            ).model_dump_json(by_alias=True)
            for name in diff.changed
        ] + [
            AppDeregisteredAnnouncement(
                timestamp=_now_iso(),
                type=DEREGISTERED_ANNOUNCEMENT_TYPE,
                event_id=_new_event_id(),
                source=ANNOUNCEMENT_SOURCE,
                app=name,
            ).model_dump_json(by_alias=True)
            for name in diff.gone
        ]
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as stream:
                stream.write("".join(line + "\n" for line in lines))
        except OSError as e:
            logger.opt(exception=e).error("Failed to announce the app registry to {}", self.events_path)
            return
        self._announced = current
        logger.info("Announced apps to the services stream: registered={} deregistered={}", diff.changed, diff.gone)

    def _archive_oversized_stream(self) -> None:
        """Move an oversized stream aside before the first write, and compress it on a thread of its own, so the
        new stream holds only the current apps and a gigabyte's compression does not hold up the shell's start."""
        try:
            if stage_oversized_announcements(self.events_path):
                logger.info("Moved the oversized services stream {} aside for archiving", self.events_path)
        except OSError as e:
            logger.opt(exception=e).warning("Failed to move the oversized services stream {} aside", self.events_path)
        # A stream a stopped shell staged but never compressed is finished here too.
        if _staged_announcements(self.events_path):
            threading.Thread(
                target=self._compress_staged_streams, daemon=True, name="app-announcements-archive"
            ).start()

    def _compress_staged_streams(self) -> None:
        try:
            compress_staged_announcements(self.events_path)
        except OSError as e:
            logger.opt(exception=e).warning("Failed to archive the staged services stream beside {}", self.events_path)
