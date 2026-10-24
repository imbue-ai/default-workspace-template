"""Recording model_call events: appending them to an event file the usage collector follows.

Every event is appended to a local file first, so recording depends on nothing
else running and never fails the model call. The files live under
``data/.apps/usage/events/``, one per origin (``<origin_kind>-<origin_id>.jsonl``);
the usage collector reads them as they grow, beside the agents' own usage event
files, and the files stay the source of truth its database is built from.

Each event is one line, written with a single append, so processes recording
for the same origin at once never interleave within a line.
"""

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger

DATA_DIR_ENV_VAR: Final[str] = "IMBUE_USAGE_DATA_DIR"
# Absolute rather than relative to the repo root: apps and their claude -p children
# do not all run from it.
DEFAULT_DATA_DIR: Final[Path] = Path.home() / "workspace" / "data" / ".apps" / "usage"
EVENTS_DIR_NAME: Final[str] = "events"
EVENT_FILE_SUFFIX: Final[str] = ".jsonl"
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^a-z0-9_.-]+")


def usage_data_dir(environ: Mapping[str, str]) -> Path:
    """The usage collector's directory: its database and the event files workspace_usage writes."""
    configured = environ.get(DATA_DIR_ENV_VAR, "").strip()
    return Path(configured) if configured else DEFAULT_DATA_DIR


def events_dir(environ: Mapping[str, str]) -> Path:
    return usage_data_dir(environ) / EVENTS_DIR_NAME


def event_file_name(event: Mapping[str, object]) -> str:
    """``<origin_kind>-<origin_id>.jsonl`` from the event's attribution, made safe as a file name."""
    attribution = event.get("attribution")
    parts = [
        str(attribution.get(key))
        for key in ("origin_kind", "origin_id")
        if isinstance(attribution, Mapping) and attribution.get(key)
    ]
    name = _UNSAFE_NAME_CHARACTERS.sub("_", "-".join(parts).lower()).strip("._-")
    return (name or "unknown") + EVENT_FILE_SUFFIX


def record_model_call(
    event: Mapping[str, object], environ: Mapping[str, str] | None = None
) -> Path | None:
    """Append ``event`` to its origin's event file. Returns the file, or None if the write failed (logged)."""
    env = os.environ if environ is None else environ
    path = events_dir(env) / event_file_name(event)
    line = json.dumps(event, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(descriptor, line.encode("utf-8"))
        finally:
            os.close(descriptor)
    except OSError as e:
        logger.warning("Could not record a model_call event in {}: {}", path, e)
        return None
    return path
