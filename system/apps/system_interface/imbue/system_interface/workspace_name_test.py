import json
from pathlib import Path
from typing import Any

from imbue.system_interface.workspace_name import FALLBACK_WORKSPACE_NAME
from imbue.system_interface.workspace_name import workspace_name_source

_AGENT_ID = "agent-0123456789abcdef0123456789abcdef"


def _write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def _environ(host_directory: Path) -> dict[str, str]:
    return {"MNGR_HOST_DIR": str(host_directory), "MNGR_AGENT_ID": _AGENT_ID}


def test_the_name_is_the_setting_then_the_services_agents_label_then_the_host_name_then_workspace(
    tmp_path: Path,
) -> None:
    """Each source is used only when every source before it has nothing, and each is read at the moment of asking,
    so a rename in minds (which rewrites the label) shows on the next read."""
    source = workspace_name_source("", _environ(tmp_path))
    agent_record = tmp_path / "agents" / _AGENT_ID / "data.json"

    assert source.resolve() == FALLBACK_WORKSPACE_NAME
    _write_json(tmp_path / "data.json", {"host_name": "bright-otter"})
    assert source.resolve() == "bright-otter"
    _write_json(agent_record, {"labels": {"is_primary": "true"}})
    assert source.resolve() == "bright-otter"
    _write_json(agent_record, {"labels": {"workspace_display_name": "Research Lab", "is_primary": "true"}})
    assert source.resolve() == "Research Lab"
    _write_json(agent_record, {"labels": {"workspace_display_name": "Renamed Lab"}})
    assert source.resolve() == "Renamed Lab"
    assert workspace_name_source("  Configured  ", _environ(tmp_path)).resolve() == "Configured"
    assert workspace_name_source("   ", _environ(tmp_path)).resolve() == "Renamed Lab"


def test_a_process_outside_a_workspace_or_with_unusable_records_is_named_workspace(tmp_path: Path) -> None:
    assert workspace_name_source("", {}).resolve() == FALLBACK_WORKSPACE_NAME
    (tmp_path / "data.json").write_text("{not json")
    _write_json(tmp_path / "agents" / _AGENT_ID / "data.json", {"labels": ["not", "a", "map"]})
    assert workspace_name_source("", _environ(tmp_path)).resolve() == FALLBACK_WORKSPACE_NAME
    # Without an agent id the host's record still names it.
    _write_json(tmp_path / "data.json", {"host_name": "bright-otter"})
    assert workspace_name_source("", {"MNGR_HOST_DIR": str(tmp_path)}).resolve() == "bright-otter"
