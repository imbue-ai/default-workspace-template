"""The services event stream: how the minds desktop learns which origins a workspace's apps expose.

Every read of the app registry is announced to ``$MNGR_AGENT_STATE_DIR/events/services/events.jsonl`` as one
``service_registered`` event per app whose registered fields (URL, label, icon) differ from the last announced,
and one ``service_deregistered`` per app that left. Only changed rows are announced: ``forward_port.py`` rewrites
the whole registry whenever any app registers, so a write says nothing about which apps moved, and an app
restarting in a loop would otherwise re-announce every app in the file on every restart. The first read after
the shell starts remembers nothing and announces every app, which is what a consumer reading the stream from its
start needs. The stream is plumbing the shell writes; it imports nothing from mngr.
"""

import os
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
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# The agent state directory mngr hands every process of the workspace; the stream lives under it.
ENV_AGENT_STATE_DIR: Final[str] = "MNGR_AGENT_STATE_DIR"
SERVICE_EVENTS_REL: Final[str] = "events/services/events.jsonl"

SERVICE_EVENT_SOURCE: Final[EventSource] = EventSource("services")
SERVICE_REGISTERED_EVENT_TYPE: Final[EventType] = EventType("service_registered")
SERVICE_DEREGISTERED_EVENT_TYPE: Final[EventType] = EventType("service_deregistered")


class ServiceRegisteredEvent(EventEnvelope):
    """A service registered its URL with the agent."""

    service: str = Field(description="The registered app name")
    url: str = Field(description="Where the app is reachable from inside the workspace")
    label: str = Field(
        default="",
        description="The app's unguessable origin label; empty for a legacy row written before labels existed",
    )
    icon: str = Field(default="", description="The app's registered SVG icon markup, verbatim; empty when none")


class ServiceDeregisteredEvent(EventEnvelope):
    """A service that was previously registered is no longer available."""

    service: str = Field(description="The app name that left the registry")


class AnnouncedRow(FrozenModel):
    """One app's registered fields: everything a registration event carries but its name."""

    url: str = Field(description="The registered URL")
    label: str = Field(description="The origin label")
    icon: str = Field(description="The icon markup, empty when none")


class ServiceEventDiff(FrozenModel):
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
def diff_announced_rows(current: dict[str, AnnouncedRow], previous: dict[str, AnnouncedRow]) -> ServiceEventDiff:
    changed = tuple(name for name, row in current.items() if previous.get(name) != row)
    gone = tuple(sorted(set(previous) - set(current)))
    return ServiceEventDiff(changed=changed, gone=gone)


def service_events_path_from_environment() -> Path | None:
    """Where the stream lives, or None outside a workspace (the variable unset)."""
    state_dir = os.environ.get(ENV_AGENT_STATE_DIR)
    if not state_dir:
        return None
    return Path(state_dir) / SERVICE_EVENTS_REL


def _new_event_id() -> EventId:
    return EventId(f"evt-{uuid4().hex}")


def _now_iso() -> IsoTimestamp:
    # The envelope carries nanosecond precision; the clock gives microseconds, padded.
    return IsoTimestamp(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z")


class ServiceEventWriter(MutableModel):
    """Appends the registration events one registry read implies; remembers what it last announced."""

    model_config = {"extra": "forbid", "frozen": False}

    events_path: Path = Field(frozen=True, description="The stream's file, created with its directories on first write")

    _announced: dict[str, AnnouncedRow] = PrivateAttr(default_factory=dict)

    def announce(self, rows: Sequence[RegistryRow]) -> None:
        """Write a registration per row whose fields changed and a deregistration per row that left, then remember
        the rows as announced. A write that fails leaves the memory untouched, so the next read announces again."""
        current = announced_rows_of(rows)
        diff = diff_announced_rows(current, self._announced)
        if not diff.changed and not diff.gone:
            return
        lines = [
            ServiceRegisteredEvent(
                timestamp=_now_iso(),
                type=SERVICE_REGISTERED_EVENT_TYPE,
                event_id=_new_event_id(),
                source=SERVICE_EVENT_SOURCE,
                service=name,
                url=current[name].url,
                label=current[name].label,
                icon=current[name].icon,
            ).model_dump_json()
            for name in diff.changed
        ] + [
            ServiceDeregisteredEvent(
                timestamp=_now_iso(),
                type=SERVICE_DEREGISTERED_EVENT_TYPE,
                event_id=_new_event_id(),
                source=SERVICE_EVENT_SOURCE,
                service=name,
            ).model_dump_json()
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
