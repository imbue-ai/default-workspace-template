"""Tests for attributing notes to chats: the transcript lines that count, the session-to-agent-to-chat mapping, and
what is shown when a link in that chain is missing."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import httpx
import pytest

from memories.attribution import AuthorKind
from memories.attribution import NoteAuthor
from memories.attribution import NoteToolUse
from memories.attribution import TranscriptSources
from memories.attribution import attribute_notes
from memories.attribution import default_transcript_sources
from memories.attribution import fetch_chat_title_by_agent_id
from memories.attribution import lines_containing
from memories.attribution import matching_lines
from memories.attribution import note_tool_uses
from memories.attribution import parse_session_history
from memories.attribution import pi_note_tool_uses
from memories.attribution import read_agent_id_by_session
from memories.attribution import read_attributions
from memories.attribution import read_note_tool_uses
from memories.attribution import read_pi_note_tool_uses

_CHAT_APP_URL = "http://chat.test"
_PROJECT_DIR_NAME = "-home-user-workspace"


def _tool_line(tool: str, file_path: str, timestamp: str | None = "2026-10-01T10:00:00Z") -> str:
    record: dict[str, object] = {
        "message": {"content": [{"type": "tool_use", "name": tool, "input": {"file_path": file_path}}]}
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return json.dumps(record)


def _use(session_id: str, file_name: str, is_write: bool, hour: int | None) -> NoteToolUse:
    at = None if hour is None else datetime(2026, 10, 1, hour, tzinfo=timezone.utc)
    return NoteToolUse(session_id=session_id, file_name=file_name, is_write=is_write, at=at)


def _sources(tmp_path: Path) -> TranscriptSources:
    notes_dir = tmp_path / "workspace" / "data" / "memories"
    notes_dir.mkdir(parents=True)
    return TranscriptSources(
        claude_config_dirs=(tmp_path / "account-a", tmp_path / "account-b", tmp_path / "missing"),
        mngr_agents_dir=tmp_path / "mngr" / "agents",
        notes_dir=notes_dir,
    )


def _write_transcript(config_dir: Path, session_id: str, lines: list[str]) -> None:
    project_dir = config_dir / "projects" / _PROJECT_DIR_NAME
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")


def _write_agent(agents_dir: Path, agent_id: str, session_ids: list[str]) -> None:
    agent_dir = agents_dir / agent_id
    agent_dir.mkdir(parents=True)
    (agent_dir / "claude_session_id_history").write_text("".join(f"{sid} startup\n" for sid in session_ids))


def _chat_app(chats: object, status: int = 200) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{_CHAT_APP_URL}/api/chats"
        return httpx.Response(status, json=chats)

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_note_tool_uses_counts_writes_edits_and_reads_of_files_in_the_notes_folder_only() -> None:
    notes_dir = Path("/home/user/workspace/data/memories")

    assert note_tool_uses(_tool_line("Write", f"{notes_dir}/units.md"), "s1", notes_dir) == [
        _use("s1", "units.md", is_write=True, hour=10)
    ]
    assert note_tool_uses(_tool_line("Edit", f"{notes_dir}/units.md"), "s1", notes_dir)[0].is_write is True
    assert note_tool_uses(_tool_line("Read", f"{notes_dir}/units.md"), "s1", notes_dir)[0].is_write is False
    assert note_tool_uses(_tool_line("Write", f"{notes_dir}/sub/units.md"), "s1", notes_dir) == []
    assert note_tool_uses(_tool_line("Write", "/home/user/workspace/README.md"), "s1", notes_dir) == []
    assert note_tool_uses(_tool_line("Bash", f"{notes_dir}/units.md"), "s1", notes_dir) == []


def test_note_tool_uses_tolerates_lines_that_are_not_tool_calls() -> None:
    notes_dir = Path("/n")

    assert note_tool_uses("not json", "s1", notes_dir) == []
    assert note_tool_uses(json.dumps(["a", "list"]), "s1", notes_dir) == []
    assert note_tool_uses(json.dumps({"message": {"content": "plain text"}}), "s1", notes_dir) == []
    assert (
        note_tool_uses(json.dumps({"message": {"content": [{"type": "text", "text": "hi"}]}}), "s1", notes_dir) == []
    )
    assert (
        note_tool_uses(
            json.dumps({"message": {"content": [{"type": "tool_use", "name": "Write", "input": "x"}]}}),
            "s1",
            notes_dir,
        )
        == []
    )
    (no_time,) = note_tool_uses(_tool_line("Write", "/n/a.md", timestamp=None), "s1", notes_dir)
    (bad_time,) = note_tool_uses(_tool_line("Write", "/n/a.md", timestamp="yesterday"), "s1", notes_dir)
    assert no_time.at is None
    assert bad_time.at is None


def test_read_note_tool_uses_reads_every_accounts_transcripts_from_every_project(tmp_path: Path) -> None:
    """A worker runs in a worktree of its own (a project folder of its own) and writes to the same notes."""
    sources = _sources(tmp_path)
    notes = sources.notes_dir
    _write_transcript(
        tmp_path / "account-a",
        "session-a",
        [
            _tool_line("Write", f"{notes}/units.md"),
            json.dumps({"type": "user", "message": {"content": "remember data/memories"}}),
            _tool_line("Write", str(tmp_path / "elsewhere.md")),
        ],
    )
    _write_transcript(tmp_path / "account-b", "session-b", [_tool_line("Read", f"{notes}/units.md")])
    worker_project = tmp_path / "account-b" / "projects" / "-home-user-worktrees-worker-1"
    worker_project.mkdir(parents=True)
    (worker_project / "session-c.jsonl").write_text(_tool_line("Write", f"{notes}/units.md") + "\n")

    uses = read_note_tool_uses(sources)

    assert sorted((use.session_id, use.file_name, use.is_write) for use in uses) == [
        ("session-a", "units.md", True),
        ("session-b", "units.md", False),
        ("session-c", "units.md", True),
    ]


def test_sessions_map_to_the_agent_whose_history_lists_them(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    assert read_agent_id_by_session(agents_dir) == {}

    _write_agent(agents_dir, "agent-1", ["s1", "s2"])
    _write_agent(agents_dir, "agent-2", ["s3"])
    (agents_dir / "agent-without-history").mkdir()

    assert read_agent_id_by_session(agents_dir) == {"s1": "agent-1", "s2": "agent-1", "s3": "agent-2"}
    assert parse_session_history("s1 startup\n\n  \ns2 resume\n") == ["s1", "s2"]


def test_chat_titles_come_from_the_chat_apps_list_of_chats() -> None:
    chats = {
        "chats": [
            {"title": "Plan the launch", "agent_ids": ["agent-1", "agent-2"]},
            "not a chat",
            {"agent_ids": ["agent-3"]},
        ]
    }
    with _chat_app(chats) as client:
        assert fetch_chat_title_by_agent_id(client, _CHAT_APP_URL) == {
            "agent-1": "Plan the launch",
            "agent-2": "Plan the launch",
            "agent-3": "",
        }
    with _chat_app(["unexpected"]) as client:
        assert fetch_chat_title_by_agent_id(client, _CHAT_APP_URL) == {}


@pytest.mark.parametrize("status", [404, 500])
def test_an_unreachable_or_failing_chat_app_gives_no_titles(status: int) -> None:
    with _chat_app({"chats": []}, status=status) as client:
        assert fetch_chat_title_by_agent_id(client, _CHAT_APP_URL) is None

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        assert fetch_chat_title_by_agent_id(client, _CHAT_APP_URL) is None


def test_attribution_keeps_each_writers_latest_write_newest_first_and_counts_other_readers() -> None:
    uses = [
        _use("s1", "units.md", is_write=True, hour=9),
        _use("s2", "units.md", is_write=True, hour=11),
        _use("s1", "units.md", is_write=True, hour=10),
        _use("s1", "units.md", is_write=True, hour=None),
        _use("s3", "units.md", is_write=True, hour=8),
        _use("s1", "units.md", is_write=False, hour=12),
        _use("s2", "units.md", is_write=False, hour=12),
        _use("s4", "units.md", is_write=False, hour=12),
        _use("s1", "role.md", is_write=False, hour=12),
    ]
    agent_id_by_session = {"s1": "agent-1", "s2": "agent-1-successor", "s3": "agent-gone"}
    chat_title_by_agent_id = {"agent-1": "Plan the launch", "agent-1-successor": "Plan the launch"}

    attributions = attribute_notes(uses, agent_id_by_session, chat_title_by_agent_id)

    units = attributions["units.md"]
    assert units.authors == (
        NoteAuthor(
            kind=AuthorKind.CHAT, chat_title="Plan the launch", at=datetime(2026, 10, 1, 11, tzinfo=timezone.utc)
        ),
        NoteAuthor(kind=AuthorKind.NOT_A_CHAT, chat_title=None, at=datetime(2026, 10, 1, 8, tzinfo=timezone.utc)),
    )
    assert units.reader_count == 1
    assert attributions["role.md"].authors == ()
    assert attributions["role.md"].reader_count == 1


def test_read_attributions_says_when_chat_names_could_not_be_read(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    _write_transcript(tmp_path / "account-a", "s1", [_tool_line("Write", f"{sources.notes_dir}/units.md")])
    _write_agent(sources.mngr_agents_dir, "agent-1", ["s1"])

    with _chat_app({"chats": [{"title": "Plan the launch", "agent_ids": ["agent-1"]}]}) as client:
        named, named_messages = read_attributions(sources, client, _CHAT_APP_URL)
    with _chat_app({}, status=503) as client:
        unnamed, unnamed_messages = read_attributions(sources, client, _CHAT_APP_URL)

    assert named["units.md"].authors[0].kind == AuthorKind.CHAT
    assert named["units.md"].authors[0].chat_title == "Plan the launch"
    assert named_messages == []
    assert unnamed["units.md"].authors[0].kind == AuthorKind.UNKNOWN
    assert unnamed["units.md"].authors[0].chat_title is None
    assert unnamed_messages == ["Chat names could not be read from the chat app, so writers are shown without them."]


def test_default_sources_read_every_account_then_the_plain_claude_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    for account in ("b-account", "a-account"):
        (tmp_path / ".minds" / "accounts" / account).mkdir(parents=True)
    (tmp_path / ".minds" / "accounts" / "index.json").write_text("{}")

    sources = default_transcript_sources(notes_dir=Path("data/memories"))

    assert sources.claude_config_dirs == (
        tmp_path / ".minds" / "accounts" / "a-account",
        tmp_path / ".minds" / "accounts" / "b-account",
        tmp_path / ".claude",
    )
    assert sources.mngr_agents_dir == tmp_path / "host" / "agents"
    assert sources.notes_dir.is_absolute()


def _pi_tool_line(tool: str, path: str, role: str = "assistant") -> str:
    return json.dumps(
        {
            "type": "message",
            "timestamp": "2026-10-01T22:44:43.581Z",
            "message": {
                "role": role,
                "content": [{"type": "toolCall", "name": tool, "arguments": {"path": path, "content": "x"}}],
            },
        }
    )


def _write_pi_session(agents_dir: Path, agent_id: str, lines: list[str], cwd: str = "/home/user/workspace") -> None:
    sessions_dir = agents_dir / agent_id / "plugin" / "pi_coding" / "sessions" / "--home-user-workspace--"
    sessions_dir.mkdir(parents=True)
    header = json.dumps({"type": "session", "version": 3, "id": "s", "cwd": cwd})
    (sessions_dir / "2026-10-01T18-58-16-632Z_s.jsonl").write_text("\n".join([header, *lines]) + "\n")


def test_pi_note_tool_uses_counts_writes_edits_and_reads_including_relative_paths() -> None:
    notes_dir = Path("/home/user/workspace/data/memories")
    cwd = Path("/home/user/workspace")

    (write,) = pi_note_tool_uses(_pi_tool_line("write", f"{notes_dir}/job.md"), "pi:a1", notes_dir, cwd)
    (edit,) = pi_note_tool_uses(_pi_tool_line("edit", "data/memories/job.md"), "pi:a1", notes_dir, cwd)
    (read,) = pi_note_tool_uses(_pi_tool_line("read", "data/memories/job.md"), "pi:a1", notes_dir, cwd)

    assert (write.file_name, write.is_write, write.session_id) == ("job.md", True, "pi:a1")
    assert write.at == datetime(2026, 10, 1, 22, 44, 43, 581000, tzinfo=timezone.utc)
    assert (edit.is_write, read.is_write) == (True, False)
    assert pi_note_tool_uses(_pi_tool_line("bash", f"{notes_dir}/job.md"), "pi:a1", notes_dir, cwd) == []
    assert pi_note_tool_uses(_pi_tool_line("write", "/elsewhere/job.md"), "pi:a1", notes_dir, cwd) == []
    assert pi_note_tool_uses(_pi_tool_line("write", f"{notes_dir}/job.md", role="user"), "pi:a1", notes_dir, cwd) == []
    assert pi_note_tool_uses("not json", "pi:a1", notes_dir, cwd) == []


def test_a_pi_chats_note_is_attributed_to_its_chat_through_the_agent_folder(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    notes = sources.notes_dir
    _write_pi_session(
        sources.mngr_agents_dir,
        "agent-pi",
        [_pi_tool_line("write", f"{notes}/user-profession.md"), _pi_tool_line("read", f"{notes}/user-location.md")],
        cwd=str(notes.parent.parent),
    )
    _write_transcript(tmp_path / "account-a", "s1", [_tool_line("Read", f"{notes}/user-profession.md")])
    _write_agent(sources.mngr_agents_dir, "agent-claude", ["s1"])

    uses, agent_id_by_session = read_pi_note_tool_uses(sources.mngr_agents_dir, notes)
    chats = {
        "chats": [
            {"title": "pi-test", "agent_ids": ["agent-pi"]},
            {"title": "claude-test", "agent_ids": ["agent-claude"]},
        ]
    }
    with _chat_app(chats) as client:
        attributions, _ = read_attributions(sources, client, _CHAT_APP_URL)

    assert agent_id_by_session == {"pi:agent-pi": "agent-pi"}
    assert [(use.file_name, use.is_write) for use in uses] == [
        ("user-profession.md", True),
        ("user-location.md", False),
    ]
    assert attributions["user-profession.md"].authors[0].chat_title == "pi-test"
    assert attributions["user-profession.md"].reader_count == 1
    assert attributions["user-location.md"].reader_count == 1


def test_a_session_no_agent_claims_is_unknown_and_a_writer_is_not_its_own_reader() -> None:
    uses = [
        _use("plain-claude", "units.md", is_write=True, hour=9),
        _use("plain-claude", "units.md", is_write=False, hour=9),
    ]

    (author,) = attribute_notes(uses, {}, {"agent-1": "Plan the launch"})["units.md"].authors

    assert author.kind == AuthorKind.UNKNOWN
    assert attribute_notes(uses, {}, {})["units.md"].reader_count == 0


def test_without_the_chat_app_every_writer_is_unknown_rather_than_a_deleted_chat() -> None:
    uses = [_use("s1", "units.md", is_write=True, hour=9)]

    (author,) = attribute_notes(uses, {"s1": "agent-1"}, None)["units.md"].authors

    assert author.kind == AuthorKind.UNKNOWN


def test_matching_lines_returns_each_line_with_a_marker_once_in_order() -> None:
    block = b'first data/memories x\nskip me\nboth data/memories and "type":"session"\nlast "type":"session"'

    assert matching_lines(block, (b"data/memories", b'"type":"session"')) == [
        "first data/memories x",
        'both data/memories and "type":"session"',
        'last "type":"session"',
    ]
    assert matching_lines(b"nothing here\n", (b"data/memories",)) == []
    assert matching_lines(b"bad \xff byte data/memories\n", (b"data/memories",)) == ["bad \ufffd byte data/memories"]


@pytest.mark.parametrize("chunk_bytes", [1, 7, 64, 4 * 1024 * 1024])
def test_lines_containing_finds_lines_split_across_chunks_and_a_last_line_without_a_newline(
    tmp_path: Path, chunk_bytes: int
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b"one\ntwo data/memories/a.md\nthree\nfour data/memories/b.md")

    assert lines_containing(transcript, (b"data/memories",), chunk_bytes) == [
        "two data/memories/a.md",
        "four data/memories/b.md",
    ]


def test_lines_containing_reads_an_empty_file_as_no_lines(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")

    assert lines_containing(empty, (b"data/memories",), 64) == []
