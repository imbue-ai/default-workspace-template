"""Tests for the routes: the page and its assets, the contract module, the notes document (with the backups'
retention and where chat names come from), correcting a note, and deleting one for good."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx
import pytest
from flask import Flask
from flask.testing import FlaskClient

from memories.attribution import DEFAULT_CHAT_APP_URL
from memories.attribution import TranscriptSources
from memories.changes import NoteChange
from memories.changes import NoteChangeKind
from memories.changes import read_changes
from memories.notes import INDEX_FILENAME
from memories.pages import build_pages_blueprint

_REGISTERED_CHAT_URL = "http://127.0.0.1:9010"
_NOW = datetime(2026, 10, 1, 22, 4, 32, tzinfo=timezone.utc)


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
            changes_path=tmp_path / "state" / "user-changes.jsonl",
            now=lambda: _NOW,
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
    assert body["notes"][0]["index_entry"] == {
        "line_number": 1,
        "title": "Units",
        "hook": "Prefers metric units",
        "is_loaded": True,
    }
    assert body["index"] == {
        "line_count": 1,
        "loaded_line_count": 1,
        "max_lines": 200,
        "max_bytes": 25600,
        "missing_files": [],
    }
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


def test_a_write_from_the_apps_own_page_behind_the_forwarding_proxy_is_allowed(tmp_path: Path) -> None:
    """The local forwarding proxy hands the app its backend address as Host, so the page's Origin never matches it;
    the browser's own Sec-Fetch-Site is what says the request came from this page."""
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        version = client.get("/api/notes").get_json()["notes"][0]["version"]

        response = client.delete(
            "/api/notes/units.md",
            json={"version": version},
            headers={
                "Host": "127.0.0.1:8050",
                "Origin": "http://memories-2vpr84gh.agent-0123456789abcdef0123456789abcdef.localhost:8421",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == 200
    assert not (tmp_path / "memories" / "units.md").exists()


@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site", "none"])
def test_a_write_from_another_page_or_without_json_is_refused(tmp_path: Path, fetch_site: str) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        version = client.get("/api/notes").get_json()["notes"][0]["version"]

        other_page = client.delete(
            "/api/notes/units.md", json={"version": version}, headers={"Sec-Fetch-Site": fetch_site}
        )
        not_json = client.delete("/api/notes/units.md", data=f'{{"version": "{version}"}}')

    assert other_page.status_code == 403
    assert not_json.status_code == 403
    assert (tmp_path / "memories" / "units.md").is_file()


def test_the_page_says_it_is_not_built_until_the_bundle_exists_and_then_serves_it(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        unbuilt = client.get("/")
        unbuilt_health = client.get("/api/health").get_json()
        (tmp_path / "static" / "assets").mkdir(parents=True)
        (tmp_path / "static" / "index.html").write_text("<!doctype html><title>Agent Memory</title>")
        (tmp_path / "static" / "assets" / "index.js").write_text("console.log(1)")
        built = client.get("/")
        built_health = client.get("/api/health").get_json()
        asset = client.get("/assets/index.js")
        missing_asset = client.get("/assets/missing.js")

    assert b"not been built" in unbuilt.data
    assert unbuilt.headers["Cache-Control"] == "no-store"
    assert unbuilt_health == {"status": "ok", "is_frontend_built": False}
    assert built.data == b"<!doctype html><title>Agent Memory</title>"
    assert built.headers["Cache-Control"] == "no-store"
    assert built_health == {"status": "ok", "is_frontend_built": True}
    assert asset.data == b"console.log(1)"
    assert missing_asset.status_code == 404


def test_the_contract_module_is_served_once_the_shell_has_built_it(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        missing = client.get("/_static/app_contract.js")
        (tmp_path / "app_contract.js").write_text("export const contract = 1;")
        served = client.get("/_static/app_contract.js")

    assert missing.status_code == 404
    assert "not built" in missing.get_json()["detail"]
    assert served.status_code == 200
    assert served.data == b"export const contract = 1;"


def test_chat_names_are_asked_of_the_chat_app_the_registry_names(tmp_path: Path) -> None:
    asked_urls: list[str] = []

    def chat_app(request: httpx.Request) -> httpx.Response:
        asked_urls.append(str(request.url))
        return httpx.Response(200, json={"chats": []})

    with httpx.Client(transport=httpx.MockTransport(chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        client.get("/api/notes")
        (tmp_path / "apps.toml").write_text(f'[[apps]]\nname = "chat"\nurl = "{_REGISTERED_CHAT_URL}/"\n')
        client.get("/api/notes")
        (tmp_path / "apps.toml").write_text("not = [valid toml")
        client.get("/api/notes")

    assert asked_urls == [
        f"{DEFAULT_CHAT_APP_URL}/api/chats",
        f"{_REGISTERED_CHAT_URL}/api/chats",
        f"{DEFAULT_CHAT_APP_URL}/api/chats",
    ]


def test_correcting_a_note_rewrites_it_and_its_index_line(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        (note,) = client.get("/api/notes").get_json()["notes"]

        response = client.put(
            "/api/notes/units.md", json={"description": "Prefers metric", "body": "Km.", "version": note["version"]}
        )

    assert response.status_code == 200
    assert response.get_json()["description"] == "Prefers metric"
    assert (tmp_path / "memories" / INDEX_FILENAME).read_text() == "- [Units](units.md) — Prefers metric\n"


def test_correcting_a_note_refuses_an_empty_summary_a_stale_version_and_names_that_are_not_notes(
    tmp_path: Path,
) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        version = client.get("/api/notes").get_json()["notes"][0]["version"]

        empty = client.put("/api/notes/units.md", json={"description": "  ", "body": "x", "version": version})
        stale = client.put("/api/notes/units.md", json={"description": "x", "body": "x", "version": "0-0"})
        not_a_note = client.put("/api/notes/MEMORY.md", json={"description": "x", "body": "x", "version": version})
        missing = client.put("/api/notes/missing.md", json={"description": "x", "body": "x", "version": version})
        no_body = client.put("/api/notes/units.md", json=["not", "an", "object"])

    assert (empty.status_code, stale.status_code, not_a_note.status_code, missing.status_code) == (400, 409, 400, 404)
    assert no_body.status_code == 400
    assert (
        (tmp_path / "memories" / "units.md")
        .read_text()
        .startswith("---\nname: units\ndescription: Prefers metric units")
    )


def test_deleting_a_missing_note_or_the_index_is_refused(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)

        missing = client.delete("/api/notes/missing.md", json={"version": "0-0"})
        index = client.delete("/api/notes/MEMORY.md", json={"version": "0-0"})

    assert missing.status_code == 404
    assert index.status_code == 400
    assert (tmp_path / "memories" / INDEX_FILENAME).is_file()


def _changes(tmp_path: Path) -> list[NoteChange]:
    return read_changes(tmp_path / "state" / "user-changes.jsonl")


def test_a_delete_and_an_edit_are_recorded_for_open_chats_without_the_notes_content(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        (note,) = client.get("/api/notes").get_json()["notes"]
        edited = client.put(
            "/api/notes/units.md", json={"description": "Prefers SI units", "body": "Km.", "version": note["version"]}
        )
        deleted = client.delete("/api/notes/units.md", json={"version": edited.get_json()["version"]})

    assert (edited.status_code, deleted.status_code) == (200, 200)
    assert _changes(tmp_path) == [
        NoteChange(file_name="units.md", change=NoteChangeKind.EDITED, at=_NOW),
        NoteChange(file_name="units.md", change=NoteChangeKind.DELETED, at=_NOW),
    ]
    record = (tmp_path / "state" / "user-changes.jsonl").read_text()
    assert "metric" not in record
    assert "SI" not in record


def test_a_refused_delete_or_edit_records_nothing(tmp_path: Path) -> None:
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        stale_edit = client.put("/api/notes/units.md", json={"description": "x", "body": "x", "version": "0-0"})
        stale_delete = client.delete("/api/notes/units.md", json={"version": "0-0"})

    assert (stale_edit.status_code, stale_delete.status_code) == (409, 409)
    assert _changes(tmp_path) == []


def test_a_delete_that_cannot_be_recorded_says_so_instead_of_claiming_success(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("a file where the record's folder should be")
    with httpx.Client(transport=httpx.MockTransport(_chat_app)) as http_client:
        client = _client(tmp_path, http_client)
        version = client.get("/api/notes").get_json()["notes"][0]["version"]
        response = client.delete("/api/notes/units.md", json={"version": version})

    assert response.status_code == 500
    assert "units.md was deleted, but open chats could not be told" in response.get_json()["detail"]
    assert not (tmp_path / "memories" / "units.md").exists()
