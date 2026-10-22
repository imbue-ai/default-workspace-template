import os
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from skills_catalog.catalog import SkillsCatalog
from skills_catalog.catalog import build_catalog
from skills_catalog.catalog import parse_skill_name
from skills_catalog.catalog import repo_root_from_environment
from skills_catalog.data_types import SkillSummary
from skills_catalog.errors import SkillNotFoundError
from skills_catalog.primitives import SKILLS_DIR
from skills_catalog.primitives import SkillAudience
from skills_catalog.primitives import SkillName
from skills_catalog.primitives import SkillSource
from skills_catalog.testing import commit_all
from skills_catalog.testing import init_workspace_repo
from skills_catalog.testing import run_git
from skills_catalog.testing import write_skill

_INSTALL_LOCK = (
    '{"version": 1, "skills": {"web-design": {"source": "vercel-labs/agent-skills", "sourceType": "github",'
    ' "skillPath": "skills/web-design/SKILL.md", "computedHash": "abc"}}}'
)


def test_who_a_skill_comes_from_follows_its_author_then_the_lock_files(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    write_skill(workspace_root, "blueprint", "Plan a change.", author="imbue")
    write_skill(workspace_root, "datalib", "Search your data.", author="Imbue")
    write_skill(workspace_root, "web-design", "Design guidelines.", author="vercel")
    write_skill(workspace_root, "trivia-quiz", "Quiz the user.", author="user")
    write_skill(workspace_root, "news-digest", "Summarize the news.")
    (workspace_root / "skills-lock.json").write_text(
        _INSTALL_LOCK.replace('"web-design"', '"blueprint": {}, "web-design"', 1)
    )
    commit_all(workspace_root, "Workspace changes")

    by_name: dict[str, SkillSummary] = {summary.name: summary for summary in catalog.list_skills().skills}

    assert {name: (summary.author, summary.source) for name, summary in by_name.items()} == {
        "assist": ("", SkillSource.WORKSPACE),
        "blueprint": ("imbue", SkillSource.STUDIO),
        "datalib": ("Imbue", SkillSource.STUDIO),
        "news-digest": ("", SkillSource.WORKSPACE),
        "notify-user": ("", SkillSource.WORKSPACE),
        "trivia-quiz": ("user", SkillSource.WORKSPACE),
        "web-design": ("vercel", SkillSource.INSTALLED),
    }


def test_a_skill_is_for_people_unless_its_frontmatter_marks_it_as_the_system_s(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    write_skill(workspace_root, "handoff-summary", "Hand a chat over.", author="imbue", audience="system")
    write_skill(workspace_root, "launch-task", "Start a worker.", author="imbue", audience="System")
    write_skill(workspace_root, "build-app", "Build an app.", author="imbue", audience="people")

    by_name: dict[str, SkillSummary] = {summary.name: summary for summary in catalog.list_skills().skills}

    assert {name: summary.audience for name, summary in by_name.items()} == {
        "assist": SkillAudience.PEOPLE,
        "build-app": SkillAudience.PEOPLE,
        "handoff-summary": SkillAudience.SYSTEM,
        "launch-task": SkillAudience.SYSTEM,
        "notify-user": SkillAudience.PEOPLE,
    }


def test_a_saved_skill_was_last_updated_by_its_newest_commit_and_an_edited_one_by_its_files(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    saved_at = run_git(workspace_root, "log", "-1", "--format=%cI").strip()
    edited_file = write_skill(workspace_root, "assist", "Edited, not saved.")
    os.utime(edited_file, (1_800_000_000, 1_800_000_000))
    # A cache written later says nothing about when the skill was edited.
    cache = edited_file.parent / "__pycache__" / "helper.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"\0")
    os.utime(cache, (1_900_000_000, 1_900_000_000))
    draft_file = write_skill(workspace_root, "draft-skill", "Never saved.")
    for path in (draft_file, draft_file.parent):
        os.utime(path, (1_700_000_000, 1_700_000_000))

    by_name: dict[str, SkillSummary] = {summary.name: summary for summary in catalog.list_skills().skills}

    assert by_name["notify-user"].last_updated_at == datetime.fromisoformat(saved_at)
    assert by_name["assist"].last_updated_at == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert by_name["draft-skill"].last_updated_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_an_installed_skill_carries_its_lock_file_source(workspace_root: Path, catalog: SkillsCatalog) -> None:
    write_skill(workspace_root, "web-design", "Design guidelines.")
    (workspace_root / "skills-lock.json").write_text(_INSTALL_LOCK)
    commit_all(workspace_root, "npx skills add vercel-labs/agent-skills")

    summary = catalog.describe_skill(SkillName("web-design")).summary

    assert summary.install_source is not None
    assert summary.install_source.source == "vercel-labs/agent-skills"
    assert summary.install_source.lock_file == Path("skills-lock.json")


def test_the_listing_reads_title_and_description_from_the_frontmatter(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    skill_file = workspace_root / SKILLS_DIR / "no-frontmatter" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# Just a body\n")

    by_name: dict[str, SkillSummary] = {summary.name: summary for summary in catalog.list_skills().skills}

    assert by_name["assist"].title == "assist"
    assert by_name["assist"].description == "The template's assist."
    assert by_name["no-frontmatter"].title == "no-frontmatter"
    assert by_name["no-frontmatter"].description == ""


def test_the_listing_skips_directories_without_a_skill_file_and_hidden_ones(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    (workspace_root / SKILLS_DIR / "references-only").mkdir()
    (workspace_root / SKILLS_DIR / "references-only" / "notes.md").write_text("notes\n")
    write_skill(workspace_root, ".hidden", "Not a skill.")
    (workspace_root / SKILLS_DIR / "README.md").write_text("the skills folder\n")

    assert [summary.name for summary in catalog.list_skills().skills] == ["assist", "notify-user"]


def test_the_listing_names_the_skills_folder_absolutely(workspace_root: Path, catalog: SkillsCatalog) -> None:
    assert catalog.list_skills().skills_directory == (workspace_root / SKILLS_DIR).absolute()


def test_a_workspace_without_a_skills_folder_lists_nothing(tmp_path: Path) -> None:
    catalog = build_catalog(init_workspace_repo(tmp_path / "empty", ()))

    assert catalog.list_skills().skills == ()


def test_a_skill_page_carries_its_file_exactly_and_its_folder(workspace_root: Path, catalog: SkillsCatalog) -> None:
    skill_directory = workspace_root / SKILLS_DIR / "assist"

    detail = catalog.describe_skill(SkillName("assist"))

    assert detail.summary.name == "assist"
    assert detail.content == (skill_directory / "SKILL.md").read_text()
    assert detail.directory == skill_directory.absolute()


def test_a_missing_skill_is_not_found(catalog: SkillsCatalog) -> None:
    with pytest.raises(SkillNotFoundError, match="no-such-skill"):
        catalog.describe_skill(SkillName("no-such-skill"))


@pytest.mark.parametrize("raw_name", ["", "..", ".hidden", "a/b", "a\\b"])
def test_a_request_cannot_name_a_path_outside_the_skills_folder(raw_name: str) -> None:
    assert parse_skill_name(raw_name) is None


def test_a_plain_directory_name_is_a_skill_name() -> None:
    assert parse_skill_name("build-app") == "build-app"


def test_the_repo_root_defaults_to_the_current_directory() -> None:
    assert repo_root_from_environment({}) == Path.cwd()
    assert repo_root_from_environment({"SKILLS_CATALOG_REPO_ROOT": "/elsewhere"}) == Path("/elsewhere")


def test_a_broken_link_in_an_edited_skill_s_folder_does_not_stop_the_listing(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    skill_directory = workspace_root / SKILLS_DIR / "assist"
    (skill_directory / "gone.md").symlink_to(workspace_root / "no-such-target.md")
    for path in (skill_directory / "gone.md", skill_directory / "SKILL.md", skill_directory):
        os.utime(path, (1_800_000_000, 1_800_000_000), follow_symlinks=False)

    by_name: dict[str, SkillSummary] = {summary.name: summary for summary in catalog.list_skills().skills}

    assert by_name["assist"].last_updated_at == datetime.fromtimestamp(1_800_000_000, tz=UTC)
