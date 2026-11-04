import json
from pathlib import Path

import pytest

from share_gateway.agent_workspace import WorkspaceCoordinates
from share_gateway.agent_workspace import app_origins_for
from share_gateway.agent_workspace import exec_origin_for
from share_gateway.agent_workspace import read_workspace_coordinates
from share_gateway.caddyfile import RegisteredApp

_APPS = [
    RegisteredApp("system_interface", "system_interface-sh111111", "127.0.0.1", 8000, is_grantable=False),
    RegisteredApp("owner-exec", "owner-exec-ex111111", "127.0.0.1", 8793, is_grantable=False),
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
    host_key = tmp_path / "host_key.pub"
    host_key.write_text("ssh-ed25519 AAAAhost root@box\n")
    environ = {
        "MNGR_HOST_DIR": str(host_dir),
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "MNGR_AGENT_ID": "agent-" + "b" * 32,
    }

    coordinates = read_workspace_coordinates(environ, host_key, fallback_display_name="alpha")

    assert coordinates.host_id == "host-" + "a" * 32
    assert coordinates.workspace_id == "agent-" + "b" * 32
    assert coordinates.display_name == "Research Lab"
    assert coordinates.host_public_key == "ssh-ed25519 AAAAhost root@box"
    assert coordinates.exec_audience("alpha.example.com") == "container:host-" + "a" * 32


def test_read_workspace_coordinates_tolerates_missing_records(tmp_path: Path) -> None:
    coordinates = read_workspace_coordinates({}, tmp_path / "absent.pub", fallback_display_name="alpha")
    assert coordinates == WorkspaceCoordinates(host_id="", workspace_id="", display_name="alpha", host_public_key="")
    assert coordinates.exec_audience("alpha.example.com") == "alpha.example.com"


def test_read_workspace_coordinates_logs_records_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text("{not json")
    host_key = tmp_path / "host_key.pub"
    host_key.mkdir()

    coordinates = read_workspace_coordinates({"MNGR_HOST_DIR": str(host_dir)}, host_key, fallback_display_name="alpha")

    assert coordinates.host_id == "" and coordinates.host_public_key == ""
    logged = capsys.readouterr().err
    assert f"Could not read the workspace record at {host_dir / 'data.json'}" in logged
    assert f"Could not read the host public key at {host_key}" in logged


def test_exec_origin_and_app_origins() -> None:
    assert exec_origin_for(_APPS, "alpha.example.com") == "https://owner-exec-ex111111.alpha.example.com"
    assert exec_origin_for([], "alpha.example.com") == ""
    assert app_origins_for(_APPS, "alpha.example.com", None) == [
        {"name": "chat", "origin": "https://chat-ch111111.alpha.example.com"},
        {"name": "files", "origin": "https://files-fi111111.alpha.example.com"},
    ]
    assert app_origins_for(_APPS, "alpha.example.com", ["files", "missing"]) == [
        {"name": "files", "origin": "https://files-fi111111.alpha.example.com"}
    ]
