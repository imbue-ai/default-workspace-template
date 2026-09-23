from pathlib import Path
from uuid import uuid4

from imbue.chat.harnesses.claude.session_files import MissingSessionScanSchedule
from imbue.chat.harnesses.claude.session_files import claude_session_ids
from imbue.chat.harnesses.claude.session_files import expected_session_file
from imbue.chat.harnesses.claude.session_files import find_session_file
from imbue.chat.harnesses.claude.session_files import move_claude_sessions
from imbue.mngr.primitives import AgentId
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name


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


def test_a_session_filed_under_its_work_dir_is_found_at_the_expected_path(tmp_path: Path) -> None:
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    session_id = uuid4().hex
    session_file = _session(tmp_path / "config", encode_claude_project_dir_name(work_dir), session_id)

    assert expected_session_file(tmp_path / "config" / "projects", session_id, str(work_dir)) == session_file
    assert expected_session_file(tmp_path / "config" / "projects", uuid4().hex, str(work_dir)) is None


def test_a_work_dir_reached_through_a_symlink_finds_the_session_claude_filed_under_its_real_path(
    tmp_path: Path,
) -> None:
    real_work_dir = tmp_path / "real-workspace"
    real_work_dir.mkdir()
    linked_work_dir = tmp_path / "linked-workspace"
    linked_work_dir.symlink_to(real_work_dir)
    session_id = uuid4().hex
    session_file = _session(tmp_path / "config", encode_claude_project_dir_name(real_work_dir.resolve()), session_id)

    assert expected_session_file(tmp_path / "config" / "projects", session_id, str(linked_work_dir)) == session_file


def test_the_scan_finds_a_session_in_any_project_dir(tmp_path: Path) -> None:
    for index in range(5):
        _session(tmp_path / "config", f"-some-other-project-{index}", uuid4().hex)
    session_id = uuid4().hex
    session_file = _session(tmp_path / "config", "-where-claude-filed-it", session_id)

    assert find_session_file(tmp_path / "config" / "projects", session_id) == session_file
    assert find_session_file(tmp_path / "config" / "projects", uuid4().hex) is None
    assert find_session_file(tmp_path / "missing-config" / "projects", session_id) is None


def test_a_missing_session_is_scanned_for_at_once_then_after_doubling_delays_up_to_the_ceiling() -> None:
    schedule = MissingSessionScanSchedule.build(first_delay_seconds=1.0, max_delay_seconds=60.0)
    session_id = uuid4().hex
    now = 5000.0

    assert schedule.is_scan_due(session_id, now)
    delays = []
    for _ in range(9):
        assert schedule.is_scan_due(session_id, now)
        delay = schedule.record_miss(session_id, now)
        delays.append(delay)
        assert not schedule.is_scan_due(session_id, now + delay - 0.01)
        now += delay
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]

    # Another id has its own schedule, and a found (forgotten) id starts over.
    assert schedule.is_scan_due(uuid4().hex, now - 100.0)
    schedule.forget(session_id)
    assert schedule.is_scan_due(session_id, now - 100.0)
    assert schedule.record_miss(session_id, now) == 1.0
