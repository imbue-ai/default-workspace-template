from pathlib import Path

import pytest

from skills_catalog.errors import GitHistoryError
from skills_catalog.git_history import GitSkillHistoryReader
from skills_catalog.testing import commit_all
from skills_catalog.testing import merge_template_update
from skills_catalog.testing import write_skill


def test_a_skill_from_the_template_commit_is_added_and_last_changed_by_the_template(workspace_root: Path) -> None:
    facts = GitSkillHistoryReader(repo_root=workspace_root).read_history().facts_by_skill_name

    assert set(facts) == {"assist", "notify-user"}
    assert facts["assist"].is_added_by_template
    assert facts["assist"].is_last_changed_by_template


def test_a_template_skill_edited_here_was_last_changed_by_this_workspace(workspace_root: Path) -> None:
    write_skill(workspace_root, "assist", "Edited in this workspace.")
    commit_all(workspace_root, "Tweak assist")

    facts = GitSkillHistoryReader(repo_root=workspace_root).read_history().facts_by_skill_name

    assert facts["assist"].is_added_by_template
    assert not facts["assist"].is_last_changed_by_template
    assert facts["notify-user"].is_last_changed_by_template


def test_a_skill_written_here_is_not_from_the_template(workspace_root: Path) -> None:
    write_skill(workspace_root, "news-digest", "Summarize the news.")
    commit_all(workspace_root, "Crystallize news-digest")

    facts = GitSkillHistoryReader(repo_root=workspace_root).read_history().facts_by_skill_name

    assert not facts["news-digest"].is_added_by_template
    assert not facts["news-digest"].is_last_changed_by_template


def test_an_update_self_merge_brings_template_skills_but_not_the_workspace_commits_before_it(
    workspace_root: Path,
) -> None:
    write_skill(workspace_root, "news-digest", "Summarize the news.")
    commit_all(workspace_root, "Crystallize news-digest")
    merge_template_update(workspace_root, ("routines-helper",), "update-self: merge template v2")

    facts = GitSkillHistoryReader(repo_root=workspace_root).read_history().facts_by_skill_name

    assert facts["routines-helper"].is_added_by_template
    assert facts["routines-helper"].is_last_changed_by_template
    # The workspace's own commit is an ancestor of the merge too, but only the upstream side is the template's.
    assert not facts["news-digest"].is_added_by_template


def test_a_merge_without_the_update_self_subject_brings_nothing_from_the_template(workspace_root: Path) -> None:
    merge_template_update(workspace_root, ("side-skill",), "Merge a side branch")

    facts = GitSkillHistoryReader(repo_root=workspace_root).read_history().facts_by_skill_name

    assert not facts["side-skill"].is_added_by_template


def test_the_history_is_read_once_per_head(workspace_root: Path) -> None:
    reader = GitSkillHistoryReader(repo_root=workspace_root)
    first = reader.read_history()

    assert reader.read_history() is first

    write_skill(workspace_root, "news-digest", "Summarize the news.")
    commit_all(workspace_root, "Crystallize news-digest")
    refreshed = reader.read_history()

    assert refreshed is not first
    assert "news-digest" in refreshed.facts_by_skill_name


def test_unsaved_changes_name_new_and_edited_skills_only(workspace_root: Path) -> None:
    write_skill(workspace_root, "draft-skill", "Not saved yet.")
    write_skill(workspace_root, "assist", "Edited but not saved.")

    names = GitSkillHistoryReader(repo_root=workspace_root).read_names_with_unsaved_changes()

    assert names == {"draft-skill", "assist"}


def test_a_directory_that_is_not_a_git_checkout_cannot_be_read(tmp_path: Path) -> None:
    with pytest.raises(GitHistoryError, match="rev-parse"):
        GitSkillHistoryReader(repo_root=tmp_path).read_history()
