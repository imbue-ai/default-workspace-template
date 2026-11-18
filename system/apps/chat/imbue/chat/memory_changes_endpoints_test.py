from pathlib import Path

import pytest
from agent_memory.changes import ChangeLog
from flask import Flask

from imbue.chat import memory_changes_endpoints


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChangeLog:
    (tmp_path / "memories").mkdir()
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path / "memories"))
    monkeypatch.setenv("AGENT_MEMORY_CHANGES_DIR", str(tmp_path / "changes"))
    return ChangeLog(changes_dir=tmp_path / "changes", memory_dir=tmp_path / "memories")


@pytest.fixture
def client():
    application = Flask("test")
    memory_changes_endpoints.register_routes(application)
    return application.test_client()


def _add_note(log: ChangeLog, chat_id: str, session_id: str, name: str) -> dict:
    log.start_turn(session_id)
    (log.memory_dir / name).write_text("text")
    [change] = log.record_tool_call(
        session_id, f"toolu-{name}", {"file_path": str(log.memory_dir / name)}, chat_id=chat_id
    )
    return change


def test_a_chat_lists_only_its_own_changes_with_the_tool_calls_that_made_them(log: ChangeLog, client) -> None:
    mine = _add_note(log, "chat-1", "s1", "tea.md")
    _add_note(log, "chat-2", "s2", "coffee.md")
    body = client.get("/api/chats/chat-1/memory-changes").get_json()
    assert [c["id"] for c in body["changes"]] == [mine["id"]]
    [change] = body["changes"]
    assert change["summary"] == 'added the note "Tea"'
    assert change["tool_use_ids"] == ["toolu-tea.md"]
    [file] = change["files"]
    assert (file["name"], file["before"], file["after"]) == ("tea.md", None, "text")
    assert (file["label"], file["after_text"], file["note_id"]) == ("New note: Tea", "text", "tea")


def test_undo_removes_the_added_note_and_marks_the_change_undone(log: ChangeLog, client) -> None:
    change = _add_note(log, "chat-1", "s1", "tea.md")
    response = client.post(f"/api/memory-changes/{change['id']}/undo", json={})
    assert response.status_code == 200
    assert response.get_json()["undone_at"] is not None
    assert not (log.memory_dir / "tea.md").exists()


def test_undo_over_a_later_edit_is_refused_until_forced(log: ChangeLog, client) -> None:
    change = _add_note(log, "chat-1", "s1", "tea.md")
    (log.memory_dir / "tea.md").write_text("edited later")
    refused = client.post(f"/api/memory-changes/{change['id']}/undo", json={})
    assert refused.status_code == 409
    assert "tea.md" in refused.get_json()["detail"]
    assert (log.memory_dir / "tea.md").exists()
    assert client.post(f"/api/memory-changes/{change['id']}/undo", json={"force": True}).status_code == 200
    assert not (log.memory_dir / "tea.md").exists()


def test_one_change_reads_back_by_id(log: ChangeLog, client) -> None:
    change = _add_note(log, "chat-1", "s1", "tea.md")
    assert client.get(f"/api/memory-changes/{change['id']}").get_json()["summary"] == 'added the note "Tea"'


def test_a_note_reads_without_its_bookkeeping_and_links_to_the_memory_app(
    log: ChangeLog, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps = tmp_path / "apps.toml"
    apps.write_text('[[apps]]\nname = "memory"\nurl = "http://localhost:8081"\nlabel = "memory-abc"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps))
    (log.memory_dir / "MEMORY.md").write_text("# Index\n")
    log.start_turn("s1")
    (log.memory_dir / "likes-tea.md").write_text(
        "---\nname: likes-tea\ndescription: User likes tea\nmetadata:\n  type: user\n---\n\nThe user likes tea.\n"
    )
    log.record_tool_call("s1", "t1", {"file_path": str(log.memory_dir / "likes-tea.md")}, chat_id="chat-1")
    (log.memory_dir / "MEMORY.md").write_text("# Index\n- [Likes tea](likes-tea.md) — user likes tea\n")
    [change] = log.record_tool_call("s1", "t2", {"file_path": str(log.memory_dir / "MEMORY.md")}, chat_id="chat-1")
    body = client.get(f"/api/memory-changes/{change['id']}").get_json()
    assert body["summary"] == 'added the note "Likes tea" and its index line'
    index, note = body["files"]
    assert note["label"] == "New note: Likes tea"
    assert note["after_text"] == "Summary: User likes tea\n\nThe user likes tea."
    assert note["details_url"] == "http://memory-abc.localhost/?note=likes-tea"
    # The index shows only this note's own line, not the rest of the index.
    assert (index["before_text"], index["after_text"]) == (None, "Likes tea — user likes tea")
    assert index["details_url"] is None
    assert body["details_url"] == "http://memory-abc.localhost/?note=likes-tea"


def test_an_unknown_change_is_a_404(log: ChangeLog, client) -> None:
    assert client.post("/api/memory-changes/nope/undo", json={}).status_code == 404
    assert client.get("/api/memory-changes/nope").status_code == 404


def test_a_change_to_one_note_s_index_line_alone_links_to_that_note(
    log: ChangeLog, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps = tmp_path / "apps.toml"
    apps.write_text('[[apps]]\nname = "memory"\nurl = "http://localhost:8081"\nlabel = "memory-abc"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps))
    (log.memory_dir / "likes-tea.md").write_text("The user likes tea.\n")
    (log.memory_dir / "MEMORY.md").write_text("# Index\n- [Tea](likes-tea.md) — tea\n- [Cake](cake.md) — cake\n")
    log.start_turn("s1")
    (log.memory_dir / "MEMORY.md").write_text("# Index\n- [Green tea](likes-tea.md) — tea\n- [Cake](cake.md) — cake\n")
    [change] = log.record_tool_call("s1", "t1", {"file_path": str(log.memory_dir / "MEMORY.md")}, chat_id="chat-1")
    body = client.get(f"/api/memory-changes/{change['id']}").get_json()
    assert body["summary"] == 'changed the index line of "Likes tea"'
    [index] = body["files"]
    assert (index["before_text"], index["after_text"]) == ("Tea — tea", "Green tea — tea")
    assert body["details_url"] == "http://memory-abc.localhost/?note=likes-tea"
    (log.memory_dir / "MEMORY.md").write_text("# Index\n- [Green tea](likes-tea.md) — tea\n- [Cake](cake.md) — lots\n")
    assert client.post(f"/api/memory-changes/{change['id']}/undo", json={}).status_code == 200
    assert (
        log.memory_dir / "MEMORY.md"
    ).read_text() == "# Index\n- [Tea](likes-tea.md) — tea\n- [Cake](cake.md) — lots\n"


def test_undo_over_a_later_edit_of_the_note_s_index_line_is_a_409(log: ChangeLog, client) -> None:
    (log.memory_dir / "MEMORY.md").write_text("# Index\n")
    log.start_turn("s1")
    (log.memory_dir / "tea.md").write_text("tea")
    (log.memory_dir / "MEMORY.md").write_text("# Index\n- [Tea](tea.md)\n")
    [change] = log.record_tool_call("s1", "t1", {"command": f"ls {log.memory_dir}"}, chat_id="chat-1")
    (log.memory_dir / "MEMORY.md").write_text("# Index\n- [Tea](tea.md) — edited\n")
    refused = client.post(f"/api/memory-changes/{change['id']}/undo", json={})
    assert refused.status_code == 409
    assert "the index" in refused.get_json()["detail"]
    assert (log.memory_dir / "tea.md").exists()
