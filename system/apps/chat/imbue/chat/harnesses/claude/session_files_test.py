from pathlib import Path
from uuid import uuid4

from imbue.chat.harnesses.claude.session_files import claude_session_ids
from imbue.chat.harnesses.claude.session_files import move_claude_sessions
from imbue.mngr.primitives import AgentId


def test_session_ids_are_the_agents_uuid_then_the_history_in_first_mention_order(tmp_path: Path) -> None:
    agent_id = str(AgentId())
    uuid = str(AgentId(agent_id).get_uuid())
    (tmp_path / "claude_session_id_history").write_text(f"{uuid} first\nsecond-session x\n{uuid} again\nthird\n")
    assert claude_session_ids(tmp_path, agent_id) == (uuid, "second-session", "third")
    # No history yet: the uuid alone; an id that is not an mngr id contributes no uuid.
    assert claude_session_ids(tmp_path / "missing", agent_id) == (uuid,)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "claude_session_id_history").write_text("only-this\n")
    assert claude_session_ids(tmp_path / "other", "agent-fixture") == ("only-this",)


def _session(config_dir: Path, project: str, session_id: str) -> Path:
    project_dir = config_dir / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)
    session_file = project_dir / f"{session_id}.jsonl"
    session_file.write_text(f'{{"session": "{session_id}"}}\n')
    (project_dir / session_id / "subagents").mkdir(parents=True)
    (project_dir / session_id / "subagents" / "sub.jsonl").write_text("{}\n")
    return session_file


def test_sessions_move_with_their_subagents_to_the_same_place_under_the_new_dir(tmp_path: Path) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    first, second = f"s-{uuid4().hex}", f"s-{uuid4().hex}"
    _session(source, "-home-user-workspace", first)
    _session(source, "-home-user-workspace", second)
    untouched = _session(source, "-home-user-workspace", f"s-{uuid4().hex}")

    moved = move_claude_sessions((first, second, "s-never-existed"), source, target)

    project_dir = target / "projects" / "-home-user-workspace"
    assert sorted(path.name for path in moved) == sorted([f"{first}.jsonl", first, f"{second}.jsonl", second])
    assert (project_dir / f"{first}.jsonl").read_text() == f'{{"session": "{first}"}}\n'
    assert (project_dir / first / "subagents" / "sub.jsonl").exists()
    assert not (source / "projects" / "-home-user-workspace" / f"{first}.jsonl").exists()
    # Another chat's session in the same tree stays where it is.
    assert untouched.exists()


def test_moving_is_idempotent_and_a_no_op_within_one_dir(tmp_path: Path) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    session_id = f"s-{uuid4().hex}"
    _session(source, "-home-user-workspace", session_id)
    assert move_claude_sessions((session_id,), source, source) == []
    first_pass = move_claude_sessions((session_id,), source, target)
    assert len(first_pass) == 2
    # Run again after a partial earlier move: nothing left at the source, nothing clobbered.
    assert move_claude_sessions((session_id,), source, target) == []
    assert (target / "projects" / "-home-user-workspace" / f"{session_id}.jsonl").exists()
    assert move_claude_sessions((session_id,), tmp_path / "nowhere", target) == []


def test_a_session_already_at_the_destination_is_left_alone(tmp_path: Path) -> None:
    source, target = tmp_path / "old", tmp_path / "new"
    session_id = f"s-{uuid4().hex}"
    _session(source, "-home-user-workspace", session_id)
    (target / "projects" / "-home-user-workspace").mkdir(parents=True)
    (target / "projects" / "-home-user-workspace" / f"{session_id}.jsonl").write_text("kept\n")

    moved = move_claude_sessions((session_id,), source, target)

    # The file stayed put on both sides; the subagent tree, absent at the destination, moved.
    assert [path.name for path in moved] == [session_id]
    assert (target / "projects" / "-home-user-workspace" / f"{session_id}.jsonl").read_text() == "kept\n"
    assert (source / "projects" / "-home-user-workspace" / f"{session_id}.jsonl").exists()
