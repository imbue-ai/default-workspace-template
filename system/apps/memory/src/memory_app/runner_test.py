"""Tests for the routes: the page and its files, the notes, the agent's changes and their undo, health, the request
guard, and the API document, over a memory folder and a change log of each test's own."""

from pathlib import Path

import pytest
from flask.testing import FlaskClient

from agent_memory.changes import ChangeLog
from agent_memory.changes import MAX_CHANGE_RECORDS
from agent_memory.changes import STALE_SESSION_SECONDS
from agent_memory.notes import NoteStore
from app_schema.testing import CROSS_SITE_FETCH_HEADERS
from app_schema.testing import assert_api_schema_describes_app
from app_schema.testing import assert_request_guard_refuses_unknown_provenance
from memory_app.runner import create_app

_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "app.toml"
_DRAFT = {"title": "Likes tea", "summary": "user likes tea", "kind": "user", "body": "The user likes tea."}


@pytest.fixture
def changes(tmp_path: Path) -> ChangeLog:
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    return ChangeLog(
        changes_dir=tmp_path / "changes",
        memory_dir=memory_dir,
        max_change_records=MAX_CHANGE_RECORDS,
        stale_session_seconds=STALE_SESSION_SECONDS,
    )


@pytest.fixture
def client(changes: ChangeLog) -> FlaskClient:
    return create_app(changes, NoteStore(changes.memory_dir)).test_client()


def _agent_writes(changes: ChangeLog, name: str, text: str) -> None:
    """What the agent's Write tool call does, recorded the way Claude's change-log hook records it."""
    (changes.memory_dir / name).write_text(text)
    changes.record_tool_call(
        "session-1",
        "tool-1",
        {"file_path": str(changes.memory_dir / name)},
        chat_id="chat-1",
        agent_name="Chat 1",
    )


def test_the_app_describes_its_api_the_way_its_manifest_says(client: FlaskClient) -> None:
    assert_api_schema_describes_app(client, _MANIFEST_PATH)


def test_the_page_is_served_under_the_document_policy_with_its_script(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "<title>Memory</title>" in page
    assert '<script type="module" src="/assets/app.js"></script>' in page
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]


def test_the_page_script_connects_to_the_shell(client: FlaskClient) -> None:
    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert script.mimetype == "text/javascript"
    assert 'import { connectToShell } from "/_static/app_contract.js"' in script.get_data(as_text=True)


def test_only_the_page_s_own_files_and_the_shell_s_files_are_served(client: FlaskClient) -> None:
    assert client.get("/assets/runner.py").status_code == 404
    assert client.get("/assets/..%2Frunner.py").status_code == 404
    assert client.get("/_static/runner.py").status_code == 404
    assert client.get("/_static/..%2Frunner.py").status_code == 404


def test_health_answers_ok(client: FlaskClient) -> None:
    assert client.get("/health").get_json() == {"status": "ok"}


def test_a_page_of_another_origin_and_a_header_less_request_off_loopback_are_refused(client: FlaskClient) -> None:
    """The request guard (app_schema.request_guard): a page of another origin cannot read or change the notes."""
    assert_request_guard_refuses_unknown_provenance(client, "GET", "/api/notes")
    cross_site = client.post("/api/notes", json=_DRAFT, headers=CROSS_SITE_FETCH_HEADERS)
    assert cross_site.status_code == 403


def test_a_note_saved_here_lists_with_its_index_line(client: FlaskClient, changes: ChangeLog) -> None:
    created = client.post("/api/notes", json=_DRAFT)
    assert created.status_code == 201
    listing = client.get("/api/notes").get_json()
    assert [note["id"] for note in listing["notes"]] == ["likes-tea"]
    assert [(entry["kind"], entry["text"]) for entry in listing["index"]["entries"]] == [("NOTE", "Likes tea")]
    assert listing["index_line_limit"] == 150
    assert listing["folder"] == str(changes.memory_dir)


def test_renaming_a_note_here_moves_its_file(client: FlaskClient, changes: ChangeLog) -> None:
    note = client.post("/api/notes", json=_DRAFT).get_json()
    saved = client.put(
        f"/api/notes/{note['id']}", json={"version": note["version"], "note": {**_DRAFT, "title": "Likes green tea"}}
    )
    assert saved.get_json()["file_name"] == "likes-green-tea.md"
    assert not (changes.memory_dir / "likes-tea.md").exists()


def test_a_save_over_a_note_changed_since_is_refused(client: FlaskClient) -> None:
    note = client.post("/api/notes", json=_DRAFT).get_json()
    refused = client.put(f"/api/notes/{note['id']}", json={"version": "stale", "note": _DRAFT})
    assert refused.status_code == 409
    assert "changed since you opened it" in refused.get_json()["detail"]


def test_a_title_with_brackets_is_refused(client: FlaskClient) -> None:
    refused = client.post("/api/notes", json={**_DRAFT, "title": "Likes [tea]"})
    assert refused.status_code == 400
    assert "square brackets" in refused.get_json()["detail"]


def test_a_deleted_note_leaves_the_listing(client: FlaskClient) -> None:
    note = client.post("/api/notes", json=_DRAFT).get_json()
    deleted = client.post(f"/api/notes/{note['id']}/delete", json={"version": note["version"]})
    assert deleted.get_json() == {"id": "likes-tea"}
    assert client.get("/api/notes").get_json()["notes"] == []
    assert client.post(f"/api/notes/{note['id']}/delete", json={"version": note["version"]}).status_code == 404


def test_an_agent_s_change_lists_newest_first_and_undoes(client: FlaskClient, changes: ChangeLog) -> None:
    changes.start_turn("session-1")
    _agent_writes(changes, "tea.md", "tea")
    changes.start_turn("session-1")
    _agent_writes(changes, "coffee.md", "coffee")
    listed = client.get("/api/changes").get_json()["changes"]
    assert [change["summary"] for change in listed] == ['added the note "Coffee"', 'added the note "Tea"']
    assert listed[0]["files"] == [{"name": "coffee.md", "before": None, "after": "coffee", "is_index_lines": False}]
    undone = client.post(f"/api/changes/{listed[0]['id']}/undo", json={})
    assert undone.status_code == 200
    assert undone.get_json()["undone_at"] is not None
    assert not (changes.memory_dir / "coffee.md").exists()


def test_undo_of_a_note_edited_since_is_refused_unless_forced(client: FlaskClient, changes: ChangeLog) -> None:
    changes.start_turn("session-1")
    _agent_writes(changes, "tea.md", "tea")
    (changes.memory_dir / "tea.md").write_text("edited since")
    (change,) = client.get("/api/changes").get_json()["changes"]
    refused = client.post(f"/api/changes/{change['id']}/undo", json={})
    assert refused.status_code == 409
    assert (
        refused.get_json()["detail"] == "Changed again since: tea.md. Undoing now would also lose those later edits."
    )
    assert (changes.memory_dir / "tea.md").read_text() == "edited since"
    assert client.post(f"/api/changes/{change['id']}/undo", json={"is_forced": True}).status_code == 200
    assert not (changes.memory_dir / "tea.md").exists()


def test_a_save_here_mid_turn_is_not_charged_to_the_agent(client: FlaskClient, changes: ChangeLog) -> None:
    changes.start_turn("session-1")
    client.post("/api/notes", json=_DRAFT)
    _agent_writes(changes, "coffee.md", "coffee")
    listed = client.get("/api/changes").get_json()["changes"]
    assert [change["note"] for change in listed] == ["coffee.md"]


@pytest.mark.parametrize("change_id", ["nope", "%2E%2E", "x%5Cy"])
def test_an_unknown_or_unsafe_change_is_not_found(client: FlaskClient, change_id: str) -> None:
    for response in (
        client.get(f"/api/changes/{change_id}"),
        client.post(f"/api/changes/{change_id}/undo", json={}),
    ):
        assert response.status_code == 404
        assert response.get_json()["detail"] == "That change is not in the record."
