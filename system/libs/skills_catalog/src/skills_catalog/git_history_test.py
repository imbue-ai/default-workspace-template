from datetime import datetime
from pathlib import Path

import pytest

from skills_catalog.errors import GitHistoryError
from skills_catalog.git_history import GitSkillHistoryReader
from skills_catalog.testing import commit_all
from skills_catalog.testing import run_git
from skills_catalog.testing import write_skill


def test_the_history_dates_each_skill_by_its_newest_commit(workspace_root: Path) -> None:
    write_skill(workspace_root, "assist", "Edited in this workspace.")
    edited_at = run_git(
        workspace_root, "log", "-1", "--format=%cI", commit_all(workspace_root, "Tweak assist")
    ).strip()

    last_changed_at = GitSkillHistoryReader(repo_root=workspace_root).read_history().last_changed_at_by_skill_name

    assert set(last_changed_at) == {"assist", "notify-user"}
    assert last_changed_at["assist"] == datetime.fromisoformat(edited_at)
    assert last_changed_at["notify-user"] <= last_changed_at["assist"]


def test_the_history_is_read_once_per_head(workspace_root: Path) -> None:
    reader = GitSkillHistoryReader(repo_root=workspace_root)
    first = reader.read_history()

    assert reader.read_history() is first

    write_skill(workspace_root, "news-digest", "Summarize the news.")
    commit_all(workspace_root, "Crystallize news-digest")
    refreshed = reader.read_history()

    assert refreshed is not first
    assert "news-digest" in refreshed.last_changed_at_by_skill_name


def test_unsaved_changes_name_new_and_edited_skills_only(workspace_root: Path) -> None:
    write_skill(workspace_root, "draft-skill", "Not saved yet.")
    write_skill(workspace_root, "assist", "Edited but not saved.")

    names = GitSkillHistoryReader(repo_root=workspace_root).read_names_with_unsaved_changes()

    assert names == {"draft-skill", "assist"}


def test_a_directory_that_is_not_a_git_checkout_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(GitHistoryError, match="rev-parse"):
        GitSkillHistoryReader(repo_root=tmp_path).read_history()
