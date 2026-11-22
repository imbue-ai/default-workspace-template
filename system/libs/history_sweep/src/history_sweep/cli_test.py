import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from imbue.mngr.primitives import AgentId

from history_sweep.cli import default_roots, main, summarize
from history_sweep.sweep import Removal, RemovalReason
from history_sweep.testing import write_archive, write_session


def _deleted_chat_session_file(tmp_path: Path, env: dict[str, str]) -> Path:
    """A just-destroyed agent's archive (with a raw transcript) and its session file in an account."""
    roots = default_roots(env, tmp_path)
    session_id = str(uuid4())
    write_archive(
        roots,
        str(AgentId.generate()),
        [session_id],
        preserved_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    return write_session(roots, session_id)[0]


@pytest.fixture
def sweep_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.chdir(tmp_path)
    return {
        "MNGR_HOST_DIR": str(tmp_path / "mngr"),
        "MINDS_ACCOUNTS_ROOT": str(tmp_path / "accounts"),
        "ROUTINES_RUN_DIR": str(tmp_path),
    }


def test_dry_run_lists_the_session_file_and_removes_nothing(
    tmp_path: Path, sweep_env: dict[str, str]
) -> None:
    session_file = _deleted_chat_session_file(tmp_path, sweep_env)

    result = CliRunner().invoke(main, ["--dry-run"], env=sweep_env)

    assert result.exit_code == 0, result.output
    assert "would remove orphaned_session_copy" in result.output
    assert str(session_file) in result.output
    assert session_file.exists()


def test_run_removes_the_session_file_and_writes_the_summary(
    tmp_path: Path, sweep_env: dict[str, str]
) -> None:
    session_file = _deleted_chat_session_file(tmp_path, sweep_env)

    result = CliRunner().invoke(main, [], env=sweep_env)

    assert result.exit_code == 0, result.output
    assert not session_file.exists()
    summary = json.loads((tmp_path / "result.json").read_text())["summary"]
    assert summary.startswith("Freed ") and "from 1 deleted chat agent(s)" in summary


def test_unreadable_chat_record_exits_non_zero_and_removes_nothing(
    tmp_path: Path, sweep_env: dict[str, str]
) -> None:
    session_file = _deleted_chat_session_file(tmp_path, sweep_env)
    record = tmp_path / "data" / ".apps" / "chat" / "chats" / "broken" / "record.json"
    record.parent.mkdir(parents=True)
    record.write_text("{not json")

    result = CliRunner().invoke(main, [], env=sweep_env)

    assert result.exit_code != 0
    assert "removed nothing" in result.output
    assert session_file.exists()


def test_default_roots_follow_the_workspace_environment(tmp_path: Path) -> None:
    roots = default_roots(
        {"MNGR_HOST_DIR": "/srv/mngr", "MINDS_ACCOUNTS_ROOT": "/srv/accounts"}, tmp_path
    )

    assert roots.host_dir == Path("/srv/mngr")
    assert roots.accounts_root == Path("/srv/accounts")
    assert roots.chats_root == Path("data/.apps/chat/chats")


def test_default_roots_fall_back_to_the_home_folder(tmp_path: Path) -> None:
    roots = default_roots({}, tmp_path)

    assert roots.host_dir == tmp_path / ".mngr"
    assert roots.accounts_root == tmp_path / ".minds" / "accounts"


def test_summary_counts_agents_and_bytes() -> None:
    agent_id = AgentId.generate()
    removals = (
        Removal(
            path=Path("/a"),
            size_bytes=1024**2,
            reason=RemovalReason.ORPHANED_SESSION_COPY,
            agent_id=agent_id,
        ),
        Removal(
            path=Path("/b"),
            size_bytes=1024**2,
            reason=RemovalReason.EXPIRED_ARCHIVE,
            agent_id=agent_id,
        ),
    )

    assert (
        summarize(removals, is_dry_run=False)
        == "Freed 2.0 MiB from 1 deleted chat agent(s)."
    )
    assert (
        summarize(removals, is_dry_run=True)
        == "Would free 2.0 MiB from 1 deleted chat agent(s)."
    )
    assert summarize((), is_dry_run=False) == "Nothing to remove."
