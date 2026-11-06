"""The workspace's own coordinates as a visiting agent needs them: ids, name, the exec service, the host key.

Everything here is read from what the services agent's environment and the
host record already carry (``MNGR_HOST_DIR/data.json`` for the host id, the
primary agent's record for its display name, the registry for the exec
service's origin label); nothing is invented.
"""

import json
import os
from collections.abc import Sequence
from pathlib import Path

from share_gateway.caddyfile import RegisteredApp
from share_gateway.log import log

EXEC_SERVICE_NAME = "owner-exec"
HOST_KEY_PUBLIC_PATH = Path("/etc/ssh/ssh_host_ed25519_key.pub")
_WORKSPACE_DISPLAY_NAME_LABEL = "workspace_display_name"


class WorkspaceCoordinates:
    """What the join answer and the ``me`` route report about this workspace."""

    def __init__(self, host_id: str, workspace_id: str, display_name: str, host_public_key: str) -> None:
        self.host_id = host_id
        self.workspace_id = workspace_id
        self.display_name = display_name
        self.host_public_key = host_public_key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkspaceCoordinates):
            return NotImplemented
        return vars(self) == vars(other)

    def __repr__(self) -> str:
        return f"WorkspaceCoordinates({vars(self)!r})"

    def exec_audience(self, workspace_domain: str) -> str:
        """owner-exec's audience: ``container:<host-id>``, or the share domain when the host id is unknown."""
        return f"container:{self.host_id}" if self.host_id else workspace_domain


def _read_json_object(path: Path) -> dict[str, object]:
    """The JSON object at ``path``; an absent file reads as empty, a record that cannot be read is logged and reads as empty."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log(f"Could not read the workspace record at {path}: {exc}")
        return {}
    if not isinstance(raw, dict):
        log(f"The workspace record at {path} is not a JSON object")
        return {}
    return raw


def _host_id_from_record(host_dir: str) -> str:
    if not host_dir:
        return ""
    value = _read_json_object(Path(host_dir) / "data.json").get("host_id")
    return value if isinstance(value, str) else ""


def _display_name_from_record(agent_state_dir: str) -> str:
    if not agent_state_dir:
        return ""
    labels = _read_json_object(Path(agent_state_dir) / "data.json").get("labels")
    if not isinstance(labels, dict):
        return ""
    value = labels.get(_WORKSPACE_DISPLAY_NAME_LABEL)
    return value if isinstance(value, str) else ""


def read_workspace_coordinates(
    environ: "os._Environ[str] | dict[str, str]",
    host_key_public_path: Path,
    fallback_display_name: str,
) -> WorkspaceCoordinates:
    """Resolve the coordinates from the environment and the records it points at; missing pieces read as ''."""
    try:
        host_public_key = host_key_public_path.read_text().strip()
    except FileNotFoundError:
        host_public_key = ""
    except OSError as exc:
        log(f"Could not read the host public key at {host_key_public_path}: {exc}")
        host_public_key = ""
    display_name = _display_name_from_record(environ.get("MNGR_AGENT_STATE_DIR", "")) or fallback_display_name
    return WorkspaceCoordinates(
        host_id=_host_id_from_record(environ.get("MNGR_HOST_DIR", "")),
        workspace_id=environ.get("MNGR_AGENT_ID", ""),
        display_name=display_name,
        host_public_key=host_public_key,
    )


def exec_origin_for(apps: Sequence[RegisteredApp], workspace_domain: str) -> str:
    """The owner-exec origin on the share, or '' while the service is not registered."""
    for app in apps:
        if app.name == EXEC_SERVICE_NAME:
            return f"https://{app.label}.{workspace_domain}"
    return ""


def app_origins_for(
    apps: Sequence[RegisteredApp],
    workspace_domain: str,
    app_names: Sequence[str] | None,
) -> list[dict[str, str]]:
    """``[{name, origin}]`` for every grantable app (``app_names`` None) or for the named ones that are registered."""
    origins: list[dict[str, str]] = []
    for app in sorted(apps, key=lambda entry: entry.name):
        if app_names is None:
            if not app.is_grantable:
                continue
        elif app.name not in app_names:
            continue
        origins.append({"name": app.name, "origin": f"https://{app.label}.{workspace_domain}"})
    return origins
