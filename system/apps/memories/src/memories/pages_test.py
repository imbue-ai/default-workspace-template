"""Tests for the notes routes: the document carries the backups' retention, and delete erases a note for good."""

from pathlib import Path

import httpx
from flask import Flask
from flask.testing import FlaskClient

from memories.attribution import TranscriptSources
from memories.notes import INDEX_FILENAME
from memories.pages import build_pages_blueprint


def _chat_app(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"chats": []})


def _client(tmp_path: Path, http_client: httpx.Client) -> FlaskClient:
    notes_dir = tmp_path / "memories"
    notes_dir.mkdir()
    (notes_dir / "units.md").write_text("---\nname: units\ndescription: Prefers metric units\n---\n\nKilometres.\n")
    (notes_dir / INDEX_FILENAME).write_text("- [Units](units.md) — Prefers metric units\n")
    (tmp_path / "backup.toml").write_text("[retention]\nkeep_monthly = 12\n")
    (tmp_path / "restic.env").write_text("RESTIC_REPOSITORY=s3:https://example.test/b\nRESTIC_PASSWORD=pw\n")
    app = Flask("memories-under-test", static_folder=None)
    app.register_blueprint(
        build_pages_blueprint(
            static_directory=tmp_path / "static",
            contract_path=tmp_path / "app_contract.js",
            notes_dir=notes_dir,
            backup_config_path=tmp_path / "backup.toml",
            restic_env_path=tmp_path / "restic.env",
            transcript_sources=TranscriptSources(
                claude_config_dirs=(),
                project_dir_name="-home-user-workspace",
                mngr_agents_dir=tmp_path / "agents",
                notes_dir=notes_dir,
            ),
            registry_path=tmp_path / "apps.toml",
            client=http_client,
        )
    )
    return app.test_client()


def test_the_notes_document_says_how_long_the_backups_keep_a_deleted_note(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        body = _client(tmp_path, http_client).get("/api/notes").get_json()

    assert body["backups"] == {
        "is_backed_up": True,
        "longest_kept": "12 months",
        "schedule": ["hourly for 24 hours", "daily for 30 days", "weekly for 12 weeks", "monthly for 12 months"],
        "settings_path": str(tmp_path / "backup.toml"),
    }
    assert [note["file_name"] for note in body["notes"]] == ["units.md"]
    assert "forgotten" not in body


def test_delete_erases_the_note_and_its_index_line(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        (note,) = client.get("/api/notes").get_json()["notes"]

        response = client.delete("/api/notes/units.md", json={"version": note["version"]})

        assert response.status_code == 200
        assert client.get("/api/notes").get_json()["notes"] == []
    assert not (tmp_path / "memories" / "units.md").exists()
    assert (tmp_path / "memories" / INDEX_FILENAME).read_text() == ""


def test_delete_answers_409_for_a_stale_version_and_keeps_the_note(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        response = _client(tmp_path, http_client).delete("/api/notes/units.md", json={"version": "0-0"})

    assert response.status_code == 409
    assert (tmp_path / "memories" / "units.md").is_file()


def test_delete_from_another_origin_or_without_json_is_refused(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        version = client.get("/api/notes").get_json()["notes"][0]["version"]

        foreign = client.delete(
            "/api/notes/units.md", json={"version": version}, headers={"Origin": "http://evil.example"}
        )
        not_json = client.delete("/api/notes/units.md", data=f'{{"version": "{version}"}}')

    assert foreign.status_code == 403
    assert not_json.status_code == 403
    assert (tmp_path / "memories" / "units.md").is_file()
