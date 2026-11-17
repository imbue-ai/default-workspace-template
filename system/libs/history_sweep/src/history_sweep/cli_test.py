from pathlib import Path

from history_sweep.cli import default_roots
from history_sweep.cli import summarize
from history_sweep.sweep import Removal
from history_sweep.sweep import RemovalReason


def test_default_roots_follow_the_workspace_environment(tmp_path: Path) -> None:
    roots = default_roots({"MNGR_HOST_DIR": "/srv/mngr", "MINDS_ACCOUNTS_ROOT": "/srv/accounts"}, tmp_path)

    assert roots.host_dir == Path("/srv/mngr")
    assert roots.accounts_root == Path("/srv/accounts")
    assert roots.chats_root == Path("data/.apps/chat/chats")


def test_default_roots_fall_back_to_the_home_folder(tmp_path: Path) -> None:
    roots = default_roots({}, tmp_path)

    assert roots.host_dir == tmp_path / ".mngr"
    assert roots.accounts_root == tmp_path / ".minds" / "accounts"


def test_summary_counts_agents_and_bytes() -> None:
    removals = (
        Removal(path=Path("/a"), size_bytes=1024**2, reason=RemovalReason.ORPHANED_SESSION_COPY, agent_id="agent-1"),
        Removal(path=Path("/b"), size_bytes=1024**2, reason=RemovalReason.EXPIRED_ARCHIVE, agent_id="agent-1"),
    )

    assert summarize(removals, is_dry_run=False) == "Freed 2.0 MiB from 1 deleted chat agent(s)."
    assert summarize(removals, is_dry_run=True) == "Would free 2.0 MiB from 1 deleted chat agent(s)."
    assert summarize((), is_dry_run=False) == "Nothing to remove."
