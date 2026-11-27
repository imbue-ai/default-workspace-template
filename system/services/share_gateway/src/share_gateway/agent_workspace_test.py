import json
from pathlib import Path

import pytest

from share_gateway.agent_workspace import WorkspaceCoordinates
from share_gateway.agent_workspace import app_origins_for
from share_gateway.agent_workspace import read_workspace_coordinates
from share_gateway.caddyfile import RegisteredApp

_APPS = [
    RegisteredApp("system_interface", "system_interface-sh111111", "127.0.0.1", 8000, is_grantable=False),
    RegisteredApp("terminal-pty", "terminal-pty-tp111111", "127.0.0.1", 7682, is_grantable=False),
    RegisteredApp("chat", "chat-ch111111", "127.0.0.1", 8010, is_grantable=True),
    RegisteredApp("files", "files-fi111111", "127.0.0.1", 8300, is_grantable=True),
]


def test_read_workspace_coordinates_from_the_records_the_environment_names(tmp_path: Path) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text(json.dumps({"host_id": "host-" + "a" * 32}))
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    (state_dir / "data.json").write_text(json.dumps({"labels": {"workspace_display_name": "Research Lab"}}))
    environ = {
        "MNGR_HOST_DIR": str(host_dir),
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "MNGR_AGENT_ID": "agent-" + "b" * 32,
    }

    coordinates = read_workspace_coordinates(environ, fallback_display_name="alpha")

    assert coordinates == WorkspaceCoordinates(
        host_id="host-" + "a" * 32, workspace_id="agent-" + "b" * 32, display_name="Research Lab"
    )


def test_read_workspace_coordinates_tolerates_missing_records() -> None:
    coordinates = read_workspace_coordinates({}, fallback_display_name="alpha")
    assert coordinates == WorkspaceCoordinates(host_id="", workspace_id="", display_name="alpha")


def test_read_workspace_coordinates_logs_records_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text("{not json")

    coordinates = read_workspace_coordinates({"MNGR_HOST_DIR": str(host_dir)}, fallback_display_name="alpha")

    assert coordinates.host_id == ""
    logged = capsys.readouterr().err
    assert f"Could not read the workspace record at {host_dir / 'data.json'}" in logged


def test_app_origins_are_the_grantable_apps_or_the_named_ones_that_are_registered() -> None:
    assert app_origins_for(_APPS, "alpha.example.com", None) == [
        {"name": "chat", "origin": "https://chat-ch111111.alpha.example.com"},
        {"name": "files", "origin": "https://files-fi111111.alpha.example.com"},
    ]
    assert app_origins_for(_APPS, "alpha.example.com", ["files", "missing"]) == [
        {"name": "files", "origin": "https://files-fi111111.alpha.example.com"}
    ]
