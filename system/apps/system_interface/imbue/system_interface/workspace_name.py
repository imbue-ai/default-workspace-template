"""The workspace's name.

The first of: the ``SYSTEM_INTERFACE_WORKSPACE_NAME`` setting; the ``workspace_display_name`` label minds puts on
the workspace's services agent (the agent whose environment supervisord, and so the shell, inherits), which a rename
in minds rewrites; the host's name in mngr's host record; and ``Workspace``. The two records are read with plain
JSON parsing and no mngr import.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.avatar.status import ENV_MNGR_HOST_DIR

ENV_MNGR_AGENT_ID: Final[str] = "MNGR_AGENT_ID"
# mngr's conventions, duplicated rather than imported: the host and agent records under the host directory, and
# the label minds writes the workspace's display name to (both records are ``data.json``).
RECORD_FILENAME: Final[str] = "data.json"
AGENTS_DIRNAME: Final[str] = "agents"
WORKSPACE_DISPLAY_NAME_LABEL: Final[str] = "workspace_display_name"
FALLBACK_WORKSPACE_NAME: Final[str] = "Workspace"


def _read_record(path: Path) -> dict[str, Any] | None:
    """A JSON object record, or None when it is absent or unusable (logged)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Could not read the workspace name from {}: {}", path, e)
        return None
    if not isinstance(raw, dict):
        logger.warning("Could not read the workspace name from {}: the record is not a JSON object", path)
        return None
    return raw


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class WorkspaceNameSource(FrozenModel):
    """Where the workspace's name is read from; read afresh on every ``resolve``, so a rename shows on the next
    page load."""

    configured_name: str = Field(description="The configured name; empty to read the mngr records")
    host_directory: Path | None = Field(description="mngr's host directory, or None outside a workspace")
    agent_id: str | None = Field(description="The services agent's id, or None outside a workspace")

    def resolve(self) -> str:
        configured = _nonempty_string(self.configured_name)
        if configured is not None:
            return configured
        if self.host_directory is None:
            return FALLBACK_WORKSPACE_NAME
        if self.agent_id is not None:
            agent = _read_record(self.host_directory / AGENTS_DIRNAME / self.agent_id / RECORD_FILENAME)
            labels = agent.get("labels") if agent is not None else None
            label = _nonempty_string(labels.get(WORKSPACE_DISPLAY_NAME_LABEL)) if isinstance(labels, dict) else None
            if label is not None:
                return label
        host = _read_record(self.host_directory / RECORD_FILENAME)
        host_name = _nonempty_string(host.get("host_name")) if host is not None else None
        return host_name if host_name is not None else FALLBACK_WORKSPACE_NAME


def workspace_name_source(configured_name: str, environ: Mapping[str, str]) -> WorkspaceNameSource:
    """The source for a process with ``environ``: its mngr host directory and agent id, when it has them."""
    host_directory = environ.get(ENV_MNGR_HOST_DIR, "")
    agent_id = environ.get(ENV_MNGR_AGENT_ID, "")
    return WorkspaceNameSource(
        configured_name=configured_name,
        host_directory=Path(host_directory) if host_directory else None,
        agent_id=agent_id if agent_id else None,
    )
