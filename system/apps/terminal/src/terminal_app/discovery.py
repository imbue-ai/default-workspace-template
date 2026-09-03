import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from app_manifest.primitives import AppName, AppUrl
from imbue.imbue_common.event_envelope import (
    EventEnvelope,
    EventId,
    EventSource,
    EventType,
    IsoTimestamp,
)
from imbue.imbue_common.pure import pure
from pydantic import Field

# The hand-written discovery stream the terminal has always appended to under the agent state
# directory (distinct from the app watcher's ``events/services`` stream).
SERVERS_EVENTS_PATH: Final[Path] = Path("events/servers/events.jsonl")
SERVER_REGISTERED_EVENT_TYPE: Final[EventType] = EventType("server_registered")
SERVERS_EVENT_SOURCE: Final[EventSource] = EventSource("servers")

_EVENT_ID_HEX_LENGTH: Final[int] = 32
_NANOSECONDS_PER_SECOND: Final[int] = 1_000_000_000


class ServerRegisteredEvent(EventEnvelope):
    """One ``server_registered`` line: which server came up at which URL."""

    server: AppName = Field(description="The registered app name")
    url: AppUrl = Field(description="Where the server listens")


@pure
def _nanosecond_timestamp(now_ns: int) -> IsoTimestamp:
    seconds, nanoseconds = divmod(now_ns, _NANOSECONDS_PER_SECOND)
    prefix = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return IsoTimestamp(f"{prefix}.{nanoseconds:09d}Z")


@pure
def build_server_registered_event(
    now_ns: int, server: AppName, url: AppUrl
) -> ServerRegisteredEvent:
    """The event as the shell script always wrote it: the id hashes the full-precision timestamp so it is unique fleet-wide."""
    timestamp = _nanosecond_timestamp(now_ns)
    digest = hashlib.sha256(f"{timestamp}:{server}:{url}".encode()).hexdigest()
    return ServerRegisteredEvent(
        timestamp=timestamp,
        type=SERVER_REGISTERED_EVENT_TYPE,
        event_id=EventId(f"evt-{digest[:_EVENT_ID_HEX_LENGTH]}"),
        source=SERVERS_EVENT_SOURCE,
        server=server,
        url=url,
    )


def write_server_registered_event(
    agent_state_dir: Path, server: AppName, url: AppUrl
) -> None:
    """Append the discovery event for ``server`` to the agent's servers stream."""
    events_path = agent_state_dir / SERVERS_EVENTS_PATH
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event = build_server_registered_event(time.time_ns(), server, url)
    with events_path.open("a", encoding="utf-8") as events_file:
        events_file.write(event.model_dump_json() + "\n")
