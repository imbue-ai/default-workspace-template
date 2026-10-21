from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from skills_catalog.data_types import InstallSource
from skills_catalog.data_types import SkillCommitFacts
from skills_catalog.primitives import SkillOrigin
from skills_catalog.provenance import LOG_FIELD_SEPARATOR
from skills_catalog.provenance import LOG_RECORD_SEPARATOR
from skills_catalog.provenance import classify_skill_origin
from skills_catalog.provenance import history_from_git_log
from skills_catalog.provenance import install_sources_from_lock_text
from skills_catalog.provenance import merge_install_sources
from skills_catalog.provenance import skill_name_of_path
from skills_catalog.provenance import skill_names_with_unsaved_changes

_ROOT_LOCK = Path("skills-lock.json")
_AGENTS_LOCK = Path(".agents/skills-lock.json")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".agents/skills/assist/SKILL.md", "assist"),
        (".agents/skills/build-app/scripts/scaffold.py", "build-app"),
        (".agents/skills/README.md", None),
        (".agents/shared/worker/SKILL.md", None),
        ("system/apps/files/app.toml", None),
    ],
)
def test_skill_name_of_path_names_the_directory_directly_under_the_skills_folder(
    path: str, expected: str | None
) -> None:
    assert skill_name_of_path(path) == expected


def _log_record(commit_hash: str, committed_at: str, paths: tuple[str, ...]) -> str:
    return f"{LOG_RECORD_SEPARATOR}{commit_hash}{LOG_FIELD_SEPARATOR}{committed_at}\n\n" + "\n".join(paths) + "\n"


def test_history_takes_the_newest_commit_as_last_change_and_the_oldest_as_the_addition() -> None:
    log_output = (
        _log_record("c3", "2026-10-03T00:00:00+00:00", (".agents/skills/assist/SKILL.md",))
        + _log_record("c2", "2026-10-02T00:00:00+00:00", (".agents/skills/report/SKILL.md",))
        + _log_record("c1", "2026-10-01T00:00:00+00:00", (".agents/skills/assist/SKILL.md", ".agents/skills/x/a.py"))
    )

    history = history_from_git_log(log_output, template_commits=frozenset({"c1"}))

    assert history.facts_by_skill_name == {
        "assist": SkillCommitFacts(
            is_added_by_template=True,
            is_last_changed_by_template=False,
            last_changed_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
        ),
        "report": SkillCommitFacts(
            is_added_by_template=False,
            is_last_changed_by_template=False,
            last_changed_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        ),
        "x": SkillCommitFacts(
            is_added_by_template=True,
            is_last_changed_by_template=True,
            last_changed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        ),
    }


def test_history_of_an_empty_log_has_no_skills() -> None:
    assert history_from_git_log("", template_commits=frozenset()).facts_by_skill_name == {}


def test_unsaved_changes_name_every_skill_a_status_entry_touches_including_a_rename_origin() -> None:
    status_output = "\0".join(
        (
            "?? .agents/skills/new-skill/SKILL.md",
            " M .agents/skills/assist/scripts/run.py",
            "R  .agents/skills/renamed/SKILL.md",
            ".agents/skills/old-name/SKILL.md",
            " M .agents/skills-lock.json",
            "",
        )
    )

    assert skill_names_with_unsaved_changes(status_output) == {"new-skill", "assist", "renamed", "old-name"}


def test_a_lock_file_records_each_skill_source() -> None:
    lock_text = (
        '{"version": 1, "skills": {"blueprint": {"source": "imbue-ai/blueprint", "sourceType": "github",'
        ' "skillPath": "skills/blueprint/SKILL.md", "computedHash": "ae26"}}}'
    )

    assert install_sources_from_lock_text(lock_text, _ROOT_LOCK) == {
        "blueprint": InstallSource(
            source="imbue-ai/blueprint",
            source_type="github",
            skill_path="skills/blueprint/SKILL.md",
            lock_file=_ROOT_LOCK,
        )
    }


@pytest.mark.parametrize(
    "lock_text",
    ["not json", "[]", '{"version": 1}', '{"skills": {"a": "not a table"}}', '{"skills": {"a": {"source": 7}}}'],
)
def test_a_malformed_lock_file_or_entry_records_nothing(lock_text: str) -> None:
    assert install_sources_from_lock_text(lock_text, _ROOT_LOCK) == {}


def test_the_first_lock_file_listing_a_skill_wins() -> None:
    def source_in(lock_file: Path) -> InstallSource:
        return InstallSource(source="owner/repo", source_type="github", skill_path="SKILL.md", lock_file=lock_file)

    merged = merge_install_sources(
        {
            _AGENTS_LOCK: {"blueprint": source_in(_AGENTS_LOCK)},
            _ROOT_LOCK: {"blueprint": source_in(_ROOT_LOCK), "x": source_in(_ROOT_LOCK)},
        }
    )

    assert merged["blueprint"].lock_file == _AGENTS_LOCK
    assert merged["x"].lock_file == _ROOT_LOCK


def _facts(is_added_by_template: bool, is_last_changed_by_template: bool) -> SkillCommitFacts:
    return SkillCommitFacts(
        is_added_by_template=is_added_by_template,
        is_last_changed_by_template=is_last_changed_by_template,
        last_changed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("facts", "is_recorded_in_lock_file", "expected"),
    [
        (None, False, SkillOrigin.NOT_SAVED),
        (None, True, SkillOrigin.NOT_SAVED),
        (_facts(True, True), False, SkillOrigin.BUILT_IN),
        (_facts(True, True), True, SkillOrigin.BUILT_IN),
        (_facts(True, False), False, SkillOrigin.CHANGED_HERE),
        (_facts(False, False), True, SkillOrigin.INSTALLED),
        (_facts(False, False), False, SkillOrigin.MADE_HERE),
    ],
)
def test_classify_skill_origin(
    facts: SkillCommitFacts | None, is_recorded_in_lock_file: bool, expected: SkillOrigin
) -> None:
    assert classify_skill_origin(facts, is_recorded_in_lock_file) == expected
