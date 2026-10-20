"""Test utilities shared across host_backup's test modules."""

from pathlib import Path

from host_backup.events import BackupEventType, make_event, write_event


def write_tick(
    events_dir: Path, *types: BackupEventType, tick_id: str, **fields: object
) -> None:
    """Append one event of each of `types` for `tick_id`, each carrying `fields`, to the
    events log, in order."""
    for event_type in types:
        write_event(events_dir, make_event(event_type, tick_id=tick_id, **fields))
