"""Who a model call is attributed to: the ``IMBUE_USAGE_CONTEXT`` a starter hands to everything it spawns.

The wire format is the one ``imbue.mngr_usage.model_call`` defines (``key=value``
pairs joined by commas, values percent-encoded, like ``OTEL_RESOURCE_ATTRIBUTES``).
It is spoken here without importing ``model_call``, which loads mngr itself, so
an app recording a call loads none of it; the usage collector's tests hold the
two implementations to the same output.

A process with no explicit context is still attributed: under supervisord,
``SUPERVISOR_PROCESS_NAME`` names the program, and the app registry maps an
app's program to its manifest name. Any other supervised program is a
workspace service (``SYSTEM``), and a process outside supervisord is
``UNKNOWN`` so the gap stays visible on the dashboard.
"""

import socket
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final
from urllib.parse import quote, unquote

from app_manifest.registry_location import registry_path
from loguru import logger

USAGE_CONTEXT_ENV_VAR: Final[str] = "IMBUE_USAGE_CONTEXT"
# A file in an mngr agent's state dir that its usage writers read ahead of the agent's
# environment, so a long-lived agent's attribution can change, e.g. per routine run.
USAGE_CONTEXT_FILENAME: Final[str] = "usage_context"
SUPERVISOR_PROCESS_NAME_ENV_VAR: Final[str] = "SUPERVISOR_PROCESS_NAME"
# The workspace is the services agent: every supervised program inherits its id.
WORKSPACE_ID_ENV_VAR: Final[str] = "MNGR_AGENT_ID"

ORIGIN_KIND: Final[str] = "origin_kind"
ORIGIN_ID: Final[str] = "origin_id"
RUN_ID: Final[str] = "run_id"
PARENT_AGENT_ID: Final[str] = "parent_agent_id"
CONTEXT_KEYS: Final[tuple[str, ...]] = (ORIGIN_KIND, ORIGIN_ID, RUN_ID, PARENT_AGENT_ID)

ORIGIN_KIND_CHAT: Final[str] = "CHAT"
ORIGIN_KIND_APP: Final[str] = "APP"
ORIGIN_KIND_ROUTINE: Final[str] = "ROUTINE"
ORIGIN_KIND_SYSTEM: Final[str] = "SYSTEM"
ORIGIN_KIND_UNKNOWN: Final[str] = "UNKNOWN"
ORIGIN_KINDS: Final[frozenset[str]] = frozenset(
    {
        ORIGIN_KIND_CHAT,
        ORIGIN_KIND_APP,
        ORIGIN_KIND_ROUTINE,
        ORIGIN_KIND_SYSTEM,
        ORIGIN_KIND_UNKNOWN,
    }
)


class UsageContextError(ValueError):
    """Raised when an ``IMBUE_USAGE_CONTEXT`` value is malformed."""


def format_usage_context(context: Mapping[str, str]) -> str:
    """Serialize a context for ``IMBUE_USAGE_CONTEXT``, in ``CONTEXT_KEYS`` order."""
    return ",".join(
        f"{key}={quote(context[key], safe='')}"
        for key in CONTEXT_KEYS
        if context.get(key)
    )


def parse_usage_context(value: str) -> dict[str, str]:
    """Parse an ``IMBUE_USAGE_CONTEXT`` value. Raises UsageContextError for a malformed one.

    A context from a newer starter still reads: a key this reader does not
    know is ignored, and an origin kind it does not know reads as ``UNKNOWN``.
    """
    context: dict[str, str] = {}
    for pair in value.split(","):
        if not pair.strip():
            continue
        key, separator, raw_value = pair.partition("=")
        if not separator:
            raise UsageContextError(
                f"{USAGE_CONTEXT_ENV_VAR} entry {pair!r} is not key=value"
            )
        if key.strip() in CONTEXT_KEYS and (parsed_value := unquote(raw_value.strip())):
            context[key.strip()] = parsed_value
    if ORIGIN_KIND not in context:
        raise UsageContextError(
            f"{USAGE_CONTEXT_ENV_VAR} value {value!r} has no {ORIGIN_KIND}"
        )
    if context[ORIGIN_KIND] not in ORIGIN_KINDS:
        return {**context, ORIGIN_KIND: ORIGIN_KIND_UNKNOWN}
    return context


def app_name_for_program(program: str, apps_file: Path) -> str | None:
    """The manifest name of the registered app ``program`` runs, or None when no app row names it."""
    try:
        registry = tomllib.loads(apps_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for row in registry.get("apps", []):
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        if row.get("program", row["name"]) == program:
            return row["name"]
    return None


def read_usage_context(
    environ: Mapping[str, str], apps_file: Path | None = None
) -> dict[str, str]:
    """The context this process's model calls are attributed to (see the module docstring)."""
    explicit = environ.get(USAGE_CONTEXT_ENV_VAR, "").strip()
    if explicit:
        try:
            return parse_usage_context(explicit)
        except UsageContextError as e:
            logger.warning("Ignored a malformed usage context: {}", e)
    program = environ.get(SUPERVISOR_PROCESS_NAME_ENV_VAR, "").strip()
    if not program:
        return {ORIGIN_KIND: ORIGIN_KIND_UNKNOWN}
    app_name = app_name_for_program(
        program, apps_file if apps_file is not None else registry_path()
    )
    if app_name is not None:
        return {ORIGIN_KIND: ORIGIN_KIND_APP, ORIGIN_ID: app_name}
    return {ORIGIN_KIND: ORIGIN_KIND_SYSTEM, ORIGIN_ID: program}


def read_workspace_id(environ: Mapping[str, str]) -> str:
    """The workspace's id: the services agent's ``MNGR_AGENT_ID``, else this host's name."""
    return environ.get(WORKSPACE_ID_ENV_VAR, "").strip() or socket.gethostname()
