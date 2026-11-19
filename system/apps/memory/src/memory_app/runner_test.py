from pathlib import Path

import pytest
from flask.testing import FlaskClient

from agent_memory.changes import ChangeLog
from agent_memory.changes import MAX_CHANGE_RECORDS
from agent_memory.changes import STALE_SESSION_SECONDS
from agent_memory.notes import NoteStore
from app_schema.testing import assert_api_schema_describes_app
from memory_app import runner
from memory_app.runner import app


def test_the_app_describes_its_api_the_way_its_manifest_says() -> None:
    assert_api_schema_describes_app(app.test_client(), Path(__file__).resolve().parents[2] / "app.toml")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    log = ChangeLog(
        changes_dir=tmp_path / "changes",
        memory_dir=memory_dir,
        max_change_records=MAX_CHANGE_RECORDS,
        stale_session_seconds=STALE_SESSION_SECONDS,
    )
    monkeypatch.setattr(runner, "CHANGES", log)
    monkeypatch.setattr(runner, "STORE", NoteStore(memory_dir))
    return app.test_client()


def _agent_writes(name: str, text: str) -> None:
    """What the agent's Write tool call does, recorded the way Claude's change-log hook records it."""
    (runner.CHANGES.memory_dir / name).write_text(text)
    runner.CHANGES.record_tool_call(
        "session-1",
        "tool-1",
        {"file_path": str(runner.CHANGES.memory_dir / name)},
        chat_id="chat-1",
        agent_name="Chat 1",
    )


_DRAFT = {"title": "Likes tea", "summary": "user likes tea", "kind": "user", "body": "The user likes tea."}


def test_a_note_saved_here_lists_with_its_index_line(client: FlaskClient) -> None:
    created = client.post("/api/notes", json=_DRAFT)
    assert created.status_code == 201
    listing = client.get("/api/notes").get_json()
    assert [note["id"] for note in listing["notes"]] == ["likes-tea"]
    assert [(entry["kind"], entry["text"]) for entry in listing["index"]["entries"]] == [("note", "Likes tea")]
    assert listing["index_line_limit"] == 150


def test_renaming_a_note_here_moves_its_file(client: FlaskClient) -> None:
    note = client.post("/api/notes", json=_DRAFT).get_json()
    saved = client.put(
        f"/api/notes/{note['id']}", json={"version": note["version"], "note": {**_DRAFT, "title": "Likes green tea"}}
    )
    assert saved.get_json()["file_name"] == "likes-green-tea.md"
    assert not (runner.CHANGES.memory_dir / "likes-tea.md").exists()


def test_a_title_with_brackets_is_refused(client: FlaskClient) -> None:
    refused = client.post("/api/notes", json={**_DRAFT, "title": "Likes [tea]"})
    assert refused.status_code == 400
    assert "square brackets" in refused.get_json()["detail"]


def test_an_agent_s_change_lists_newest_first_and_undoes(client: FlaskClient) -> None:
    runner.CHANGES.start_turn("session-1")
    _agent_writes("tea.md", "tea")
    runner.CHANGES.start_turn("session-1")
    _agent_writes("coffee.md", "coffee")
    changes = client.get("/api/changes").get_json()["changes"]
    assert [change["summary"] for change in changes] == ['added the note "Coffee"', 'added the note "Tea"']
    assert changes[0]["files"] == [{"name": "coffee.md", "before": None, "after": "coffee", "is_index_lines": False}]
    undone = client.post(f"/api/changes/{changes[0]['id']}/undo", json={})
    assert undone.status_code == 200
    assert undone.get_json()["undone_at"] is not None
    assert not (runner.CHANGES.memory_dir / "coffee.md").exists()


def test_undo_of_a_note_edited_since_is_refused_unless_forced(client: FlaskClient) -> None:
    runner.CHANGES.start_turn("session-1")
    _agent_writes("tea.md", "tea")
    (runner.CHANGES.memory_dir / "tea.md").write_text("edited since")
    (change,) = client.get("/api/changes").get_json()["changes"]
    refused = client.post(f"/api/changes/{change['id']}/undo", json={})
    assert refused.status_code == 409
    assert (runner.CHANGES.memory_dir / "tea.md").read_text() == "edited since"
    assert client.post(f"/api/changes/{change['id']}/undo", json={"is_forced": True}).status_code == 200
    assert not (runner.CHANGES.memory_dir / "tea.md").exists()


def test_a_save_here_mid_turn_is_not_charged_to_the_agent(client: FlaskClient) -> None:
    runner.CHANGES.start_turn("session-1")
    client.post("/api/notes", json=_DRAFT)
    _agent_writes("coffee.md", "coffee")
    changes = client.get("/api/changes").get_json()["changes"]
    assert [change["note"] for change in changes] == ["coffee.md"]


def test_an_unknown_change_is_not_found(client: FlaskClient) -> None:
    assert client.get("/api/changes/nope").status_code == 404
    assert client.post("/api/changes/../x/undo", json={}).status_code == 404
