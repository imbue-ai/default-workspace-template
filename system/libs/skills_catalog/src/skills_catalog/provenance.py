import json
from collections.abc import Mapping
from collections.abc import Set
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from skills_catalog.data_types import InstallSource
from skills_catalog.data_types import SkillCommitFacts
from skills_catalog.data_types import SkillsHistory
from skills_catalog.primitives import SKILLS_DIR
from skills_catalog.primitives import SkillOrigin

# Separators for the one `git log` read of the skills directory: a record per commit, then the hash and date.
LOG_RECORD_SEPARATOR: Final[str] = "\x1e"
LOG_FIELD_SEPARATOR: Final[str] = "\x1f"
LOG_FORMAT: Final[str] = f"{LOG_RECORD_SEPARATOR}%H{LOG_FIELD_SEPARATOR}%cI"
_SKILLS_PREFIX: Final[str] = f"{SKILLS_DIR.as_posix()}/"
# The porcelain status codes whose entry is followed by the path the change came from.
_STATUS_CODES_WITH_ORIGIN_PATH: Final[frozenset[str]] = frozenset({"R", "C"})


@pure
def skill_name_of_path(repo_relative_path: str) -> str | None:
    """The skill directory a repo-relative path lies in, or None outside .agents/skills/<name>/."""
    if not repo_relative_path.startswith(_SKILLS_PREFIX):
        return None
    name, separator, _ = repo_relative_path[len(_SKILLS_PREFIX) :].partition("/")
    return name if separator and name else None


@pure
def history_from_git_log(log_output: str, template_commits: Set[str]) -> SkillsHistory:
    """The facts per skill from `git log --format=LOG_FORMAT --name-only` output, newest commit first."""
    newest_by_name: dict[str, tuple[str, datetime]] = {}
    oldest_commit_by_name: dict[str, str] = {}
    for record in log_output.split(LOG_RECORD_SEPARATOR):
        header, _, body = record.strip().partition("\n")
        commit_hash, separator, committed_at = header.partition(LOG_FIELD_SEPARATOR)
        if not separator:
            continue
        names_or_none = {skill_name_of_path(line.strip()) for line in body.splitlines()}
        for name in (candidate for candidate in names_or_none if candidate is not None):
            newest_by_name.setdefault(name, (commit_hash, datetime.fromisoformat(committed_at)))
            oldest_commit_by_name[name] = commit_hash
    return SkillsHistory(
        facts_by_skill_name={
            name: SkillCommitFacts(
                is_added_by_template=oldest_commit_by_name[name] in template_commits,
                is_last_changed_by_template=newest_commit in template_commits,
                last_changed_at=newest_at,
            )
            for name, (newest_commit, newest_at) in newest_by_name.items()
        }
    )


@pure
def skill_names_with_unsaved_changes(status_output: str) -> frozenset[str]:
    """The skills a `git status --porcelain -z` of the skills directory reports a change in."""
    names: set[str] = set()
    entries = iter(status_output.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        name = skill_name_of_path(entry[3:])
        if name is not None:
            names.add(name)
        if entry[0] in _STATUS_CODES_WITH_ORIGIN_PATH:
            origin_name = skill_name_of_path(next(entries, ""))
            if origin_name is not None:
                names.add(origin_name)
    return frozenset(names)


@pure
def install_sources_from_lock_text(lock_text: str, lock_file: Path) -> dict[str, InstallSource]:
    """The install source per skill name a skills lock file records; empty for a file that is not one."""
    try:
        parsed = json.loads(lock_text)
    except ValueError as e:
        logger.warning("Ignoring the skills lock file {}, which is not JSON: {}", lock_file, e)
        return {}
    skills = parsed.get("skills") if isinstance(parsed, dict) else None
    if not isinstance(skills, dict):
        logger.warning("Ignoring the skills lock file {}, which has no skills table", lock_file)
        return {}
    sources: dict[str, InstallSource] = {}
    for name, entry in skills.items():
        if not isinstance(entry, dict):
            logger.warning("Ignoring the entry for {} in {}, which is not a table", name, lock_file)
            continue
        try:
            sources[name] = InstallSource(
                source=entry.get("source", ""),
                source_type=entry.get("sourceType", ""),
                skill_path=entry.get("skillPath", ""),
                lock_file=lock_file,
            )
        except ValidationError as e:
            logger.warning("Ignoring the entry for {} in {}: {}", name, lock_file, e)
    return sources


@pure
def merge_install_sources(
    sources_by_lock_file: Mapping[Path, Mapping[str, InstallSource]],
) -> dict[str, InstallSource]:
    """One install source per skill across the lock files, the first lock file listing a skill winning."""
    merged: dict[str, InstallSource] = {}
    for sources in sources_by_lock_file.values():
        for name, source in sources.items():
            merged.setdefault(name, source)
    return merged


@pure
def classify_skill_origin(facts: SkillCommitFacts | None, is_recorded_in_lock_file: bool) -> SkillOrigin:
    if facts is None:
        return SkillOrigin.NOT_SAVED
    if facts.is_added_by_template:
        return SkillOrigin.BUILT_IN if facts.is_last_changed_by_template else SkillOrigin.CHANGED_HERE
    return SkillOrigin.INSTALLED if is_recorded_in_lock_file else SkillOrigin.MADE_HERE
