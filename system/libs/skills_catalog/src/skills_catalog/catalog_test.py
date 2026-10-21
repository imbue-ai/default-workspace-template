from pathlib import Path

import pytest

from skills_catalog.catalog import MAX_LISTED_FILES
from skills_catalog.catalog import SkillsCatalog
from skills_catalog.catalog import build_catalog
from skills_catalog.catalog import parse_skill_name
from skills_catalog.catalog import repo_root_from_environment
from skills_catalog.data_types import SkillSummary
from skills_catalog.errors import SkillNotFoundError
from skills_catalog.primitives import SKILLS_DIR
from skills_catalog.primitives import SkillName
from skills_catalog.primitives import SkillOrigin
from skills_catalog.testing import commit_all
from skills_catalog.testing import init_workspace_repo
from skills_catalog.testing import write_skill

_INSTALL_LOCK = (
    '{"version": 1, "skills": {"web-design": {"source": "vercel-labs/agent-skills", "sourceType": "github",'
    ' "skillPath": "skills/web-design/SKILL.md", "computedHash": "abc"}}}'
)


def _origin_by_name(catalog: SkillsCatalog) -> dict[str, SkillOrigin]:
    return {summary.name: summary.origin for summary in catalog.list_skills().skills}


def test_the_listing_classifies_every_kind_of_skill(workspace_root: Path, catalog: SkillsCatalog) -> None:
    write_skill(workspace_root, "assist", "Edited in this workspace.")
    write_skill(workspace_root, "news-digest", "Summarize the news.")
    write_skill(workspace_root, "web-design", "Design guidelines.")
    (workspace_root / "skills-lock.json").write_text(_INSTALL_LOCK)
    commit_all(workspace_root, "Workspace changes")
    write_skill(workspace_root, "draft-skill", "Not saved yet.")

    assert _origin_by_name(catalog) == {
        "assist": SkillOrigin.CHANGED_HERE,
        "draft-skill": SkillOrigin.NOT_SAVED,
        "news-digest": SkillOrigin.MADE_HERE,
        "notify-user": SkillOrigin.BUILT_IN,
        "web-design": SkillOrigin.INSTALLED,
    }


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
    assert by_name["assist"].problems == ()
    assert by_name["assist"].has_unsaved_changes is False
    assert by_name["assist"].last_changed_at is not None
    assert by_name["no-frontmatter"].title == "no-frontmatter"
    assert by_name["no-frontmatter"].description == ""
    assert by_name["no-frontmatter"].has_unsaved_changes is True
    assert by_name["no-frontmatter"].last_changed_at is None
    assert by_name["no-frontmatter"].problems == (
        "frontmatter name None must equal the directory name 'no-frontmatter'",
        "frontmatter needs a description",
    )


def test_the_listing_skips_directories_without_a_skill_file_and_hidden_ones(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    (workspace_root / SKILLS_DIR / "references-only").mkdir()
    (workspace_root / SKILLS_DIR / "references-only" / "notes.md").write_text("notes\n")
    write_skill(workspace_root, ".hidden", "Not a skill.")
    (workspace_root / SKILLS_DIR / "README.md").write_text("the skills folder\n")

    assert set(_origin_by_name(catalog)) == {"assist", "notify-user"}


def test_a_workspace_without_a_skills_folder_lists_nothing(tmp_path: Path) -> None:
    catalog = build_catalog(init_workspace_repo(tmp_path / "empty", ()))

    assert catalog.list_skills().skills == ()


def test_a_skill_page_carries_its_file_exactly_and_its_files_without_caches(
    workspace_root: Path, catalog: SkillsCatalog
) -> None:
    skill_directory = workspace_root / SKILLS_DIR / "assist"
    (skill_directory / "scripts").mkdir()
    (skill_directory / "scripts" / "run.py").write_text("print('hi')\n")
    (skill_directory / "scripts" / "__pycache__").mkdir()
    (skill_directory / "scripts" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\0")

    detail = catalog.describe_skill(SkillName("assist"))

    assert detail.content == (skill_directory / "SKILL.md").read_text()
    assert detail.skill_file_path == Path(".agents/skills/assist/SKILL.md")
    assert [(file.path, file.size_bytes) for file in detail.files] == [
        (Path("SKILL.md"), len(detail.content.encode())),
        (Path("scripts/run.py"), len("print('hi')\n")),
    ]
    assert detail.is_file_list_truncated is False


def test_a_skill_page_lists_at_most_the_file_limit(workspace_root: Path, catalog: SkillsCatalog) -> None:
    data_directory = workspace_root / SKILLS_DIR / "assist" / "data"
    data_directory.mkdir()
    for idx in range(MAX_LISTED_FILES):
        (data_directory / f"{idx:04d}.txt").write_text("x")

    detail = catalog.describe_skill(SkillName("assist"))

    assert len(detail.files) == MAX_LISTED_FILES
    assert detail.is_file_list_truncated is True


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
