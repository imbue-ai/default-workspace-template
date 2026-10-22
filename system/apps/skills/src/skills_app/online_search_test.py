from pathlib import Path

import pytest

from skills_app.data_types import OnlineSkillState
from skills_app.errors import MalformedSkillIndexAnswerError
from skills_app.errors import SkillIndexUnavailableError
from skills_app.online_search import SkillsShSearcher
from skills_app.online_search import indexed_skills_from_answer
from skills_app.online_search import online_skills
from skills_app.testing import LocalSkillIndex
from skills_app.testing import indexed_skill
from skills_catalog.catalog import build_catalog
from skills_catalog.testing import commit_all
from skills_catalog.testing import init_workspace_repo
from skills_catalog.testing import write_skill

# The shape skills.sh answered for "pdf", fields the page does not read included.
_ANSWER = {
    "query": "pdf",
    "searchType": "fuzzy",
    "skills": [
        {
            "id": "anthropics/skills/docx",
            "source": "anthropics/skills",
            "skillId": "docx",
            "name": "docx",
            "installs": 205002,
        },
        {
            "id": "anthropics/skills/pdf",
            "source": "anthropics/skills",
            "skillId": "pdf",
            "name": "pdf",
            "installs": 214612,
        },
        {"id": "no-name/skills/x", "source": "no-name/skills", "installs": 3},
        "not an entry",
    ],
    "count": 4,
}


def test_an_answer_s_matches_are_read_most_installed_first_and_malformed_ones_left_out() -> None:
    matches = indexed_skills_from_answer(_ANSWER)

    assert [(match.name, match.source, match.installs, match.page_id) for match in matches] == [
        ("pdf", "anthropics/skills", 214612, "anthropics/skills/pdf"),
        ("docx", "anthropics/skills", 205002, "anthropics/skills/docx"),
    ]


def test_an_answer_without_a_skills_list_is_not_a_search_result() -> None:
    with pytest.raises(MalformedSkillIndexAnswerError):
        indexed_skills_from_answer({"error": "rate limited"})


def test_a_match_is_installed_only_when_the_skill_of_its_name_came_from_its_source(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    init_workspace_repo(root, ("assist",))
    write_skill(root, "pdf", "Work with PDFs.")
    (root / "skills-lock.json").write_text(
        '{"version": 1, "skills": {"pdf": {"source": "Anthropics/Skills", "sourceType": "github",'
        ' "skillPath": "skills/pdf/SKILL.md", "computedHash": "abc"}}}'
    )
    commit_all(root, "npx skills add anthropics/skills")
    installed = build_catalog(root).list_skills().skills

    marked = online_skills(
        (
            indexed_skill("anthropics/skills", "PDF", installs=1000),
            indexed_skill("someone/else", "assist", installs=1000),
            indexed_skill("anthropics/skills", "docx", installs=1000),
        ),
        installed,
    )

    assert [skill.state for skill in marked] == [
        OnlineSkillState.INSTALLED,
        OnlineSkillState.NAME_TAKEN,
        OnlineSkillState.AVAILABLE,
    ]
    assert marked[2].install_command == "npx skills add anthropics/skills@docx"
    assert marked[2].page_url == "https://skills.sh/anthropics/skills/docx"


def test_the_searcher_asks_the_index_once_per_query_while_its_answer_is_kept(
    local_skill_index: LocalSkillIndex,
) -> None:
    local_skill_index.answer = _ANSWER
    searcher = SkillsShSearcher(base_url=local_skill_index.base_url)

    first = searcher.search("PDF ")
    second = searcher.search("pdf")

    assert [match.name for match in first] == ["pdf", "docx"]
    assert second == first
    assert local_skill_index.requested_paths == ["/api/search?q=pdf&limit=20"]


def test_the_searcher_raises_when_the_index_answers_an_error(local_skill_index: LocalSkillIndex) -> None:
    local_skill_index.status = 503

    with pytest.raises(SkillIndexUnavailableError, match="answered 503"):
        SkillsShSearcher(base_url=local_skill_index.base_url).search("pdf")


def test_the_searcher_raises_when_the_index_answers_something_else(local_skill_index: LocalSkillIndex) -> None:
    local_skill_index.answer = {"error": "rate limited"}

    with pytest.raises(MalformedSkillIndexAnswerError):
        SkillsShSearcher(base_url=local_skill_index.base_url).search("docx")


def test_the_searcher_raises_when_the_index_cannot_be_reached() -> None:
    with pytest.raises(SkillIndexUnavailableError, match="Could not search"):
        SkillsShSearcher(base_url="http://127.0.0.1:9").search("pdf")
