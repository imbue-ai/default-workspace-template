"""Tests for the entry point's wiring: the arguments the config yields, the page app it builds, and what an
unregistered boot leaves alone."""

import json
import socket
import urllib.request
from pathlib import Path

import httpx
import pytest

from app_manifest.registry import ENV_APPS_FILE
from host_backup.config import BACKUP_TOML_PATH
from host_backup.config import RESTIC_ENV_PATH
from memories.config import Config
from memories.config import load_config
from memories.main import MANIFEST_PATH
from memories.main import MemoriesArguments
from memories.main import arguments_from_config
from memories.main import build_pages_app
from memories.main import run_memories_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _no_chat_app() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))


def _arguments(tmp_path: Path, *, port: int, is_registered: bool) -> MemoriesArguments:
    config = Config(
        memories_port=port,
        memories_notes_dir=tmp_path / "memories",
        memories_backup_config_path=tmp_path / "backup.toml",
        memories_restic_env_path=tmp_path / "restic.env",
        memories_changes_path=tmp_path / "user-changes.jsonl",
    )
    return arguments_from_config(config, MANIFEST_PATH, tmp_path / "static", is_registered=is_registered)


def test_the_config_defaults_to_the_workspace_paths_and_reads_overrides_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = load_config()
    assert defaults.memories_port == 8050
    assert defaults.memories_notes_dir == Path("data/memories")
    assert defaults.memories_backup_config_path == BACKUP_TOML_PATH
    assert defaults.memories_restic_env_path == RESTIC_ENV_PATH
    assert defaults.memories_changes_path == Path("data/.state/memories/user-changes.jsonl")

    monkeypatch.setenv("MEMORIES_PORT", "8123")
    monkeypatch.setenv("MEMORIES_NOTES_DIR", "/elsewhere/memories")
    overridden = load_config()
    assert overridden.memories_port == 8123
    assert overridden.memories_notes_dir == Path("/elsewhere/memories")


def test_arguments_come_from_the_config_and_the_flags(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path, port=8123, is_registered=True)

    assert arguments.app_url == "http://localhost:8123"
    assert arguments.host == "127.0.0.1"
    assert arguments.notes_dir == tmp_path / "memories"
    assert arguments.backup_config_path == tmp_path / "backup.toml"
    assert arguments.restic_env_path == tmp_path / "restic.env"
    assert arguments.changes_path == tmp_path / "user-changes.jsonl"
    assert arguments.is_registered is True
    assert _arguments(tmp_path, port=8123, is_registered=False).is_registered is False


def test_the_page_app_serves_the_notes_with_the_configured_backups(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path, port=8123, is_registered=True)
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "units.md").write_text("---\ndescription: Prefers metric units\n---\n\nKilometres.\n")

    with _no_chat_app() as http_client:
        client = build_pages_app(arguments, http_client).test_client()
        health = client.get("/api/health").get_json()
        body = client.get("/api/notes").get_json()

    assert health == {"status": "ok", "is_frontend_built": False}
    assert [note["description"] for note in body["notes"]] == ["Prefers metric units"]
    assert body["backups"]["is_backed_up"] is False
    assert body["backups"]["settings_path"] == str(tmp_path / "backup.toml")


def test_an_unregistered_run_serves_the_page_but_leaves_the_registry_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "apps.toml"
    monkeypatch.setenv(ENV_APPS_FILE, str(registry_path))
    port = _free_port()
    arguments = _arguments(tmp_path, port=port, is_registered=False)
    seen: dict[str, object] = {}

    def observe_and_stop() -> int:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/notes", timeout=5) as response:
            seen["status"] = response.status
            seen["notes"] = json.loads(response.read())["notes"]
        return 130

    assert run_memories_app(arguments, observe_and_stop) == 130

    assert seen == {"status": 200, "notes": []}
    assert not registry_path.exists()
