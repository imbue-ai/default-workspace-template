import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from skills_catalog.data_types import InstallSource
from skills_catalog.data_types import SkillsHistory
from skills_catalog.primitives import SKILLS_DIR
from skills_catalog.primitives import STUDIO_AUTHOR
from skills_catalog.primitives import STUDIO_REPOSITORY_OWNER
from skills_catalog.primitives import SYSTEM_AUDIENCE
from skills_catalog.primitives import SkillAudience
from skills_catalog.primitives import SkillSource

# Separators for the one `git log` read of the skills directory: a record per commit, then the hash and date.
LOG_RECORD_SEPARATOR: Final[str] = "\x1e"
LOG_FIELD_SEPARATOR: Final[str] = "\x1f"
LOG_FORMAT: Final[str] = f"{LOG_RECORD_SEPARATOR}%H{LOG_FIELD_SEPARATOR}%cI"
_SKILLS_PREFIX: Final[str] = f"{SKILLS_DIR.as_posix()}/"
# The porcelain status codes, in either the index's column or the worktree's, whose entry is followed by the path the
# change came from.
_STATUS_CODES_WITH_ORIGIN_PATH: Final[frozenset[str]] = frozenset({"R", "C"})


@pure
def skill_name_of_path(repo_relative_path: str) -> str | None:
    """The skill directory a repo-relative path lies in, or None outside .agents/skills/<name>/."""
    if not repo_relative_path.startswith(_SKILLS_PREFIX):
        return None
    name, separator, _ = repo_relative_path[len(_SKILLS_PREFIX) :].partition("/")
    return name if separator and name else None


@pure
def history_from_git_log(log_output: str) -> SkillsHistory:
    """When a commit last changed each skill, from `git log --format=LOG_FORMAT --name-only` output, newest first."""
    last_changed_at_by_skill_name: dict[str, datetime] = {}
    for record in log_output.split(LOG_RECORD_SEPARATOR):
        header, _, body = record.strip().partition("\n")
        _, separator, committed_at = header.partition(LOG_FIELD_SEPARATOR)
        if not separator:
            continue
        names_or_none = {skill_name_of_path(line.strip()) for line in body.splitlines()}
        for name in (candidate for candidate in names_or_none if candidate is not None):
            last_changed_at_by_skill_name.setdefault(name, datetime.fromisoformat(committed_at))
    return SkillsHistory(last_changed_at_by_skill_name=last_changed_at_by_skill_name)


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
        if not _STATUS_CODES_WITH_ORIGIN_PATH.isdisjoint(entry[:2]):
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
        logger.warning("Ignored the skills lock file {}, which is not JSON: {}", lock_file, e)
        return {}
    skills = parsed.get("skills") if isinstance(parsed, dict) else None
    if not isinstance(skills, dict):
        logger.warning("Ignored the skills lock file {}, which has no skills table", lock_file)
        return {}
    sources: dict[str, InstallSource] = {}
    for name, entry in skills.items():
        if not isinstance(entry, dict):
            logger.warning("Ignored the entry for {} in {}, which is not a table", name, lock_file)
            continue
        try:
            sources[name] = InstallSource(
                source=entry.get("source", ""),
                source_type=entry.get("sourceType", ""),
                skill_path=entry.get("skillPath", ""),
                lock_file=lock_file,
            )
        except ValidationError as e:
            logger.warning("Ignored the entry for {} in {}: {}", name, lock_file, e)
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
def classify_skill_source(author: str, install_source: InstallSource | None) -> SkillSource:
    """A skill's frontmatter is its own to write, so an installed one is Studio's only when it also came from one of
    Studio's repositories: a skill from anywhere else that names imbue its author is still installed."""
    is_by_studio = author.strip().lower() == STUDIO_AUTHOR
    if install_source is None:
        return SkillSource.STUDIO if is_by_studio else SkillSource.WORKSPACE
    owner = install_source.source.partition("/")[0].strip().lower()
    return SkillSource.STUDIO if is_by_studio and owner == STUDIO_REPOSITORY_OWNER else SkillSource.INSTALLED


@pure
def classify_skill_audience(audience: str, source: SkillSource) -> SkillAudience:
    """Only Studio's own skills can be its procedures: one from anywhere else is listed for people, so it cannot fold
    itself out of sight."""
    is_system = source == SkillSource.STUDIO and audience.strip().lower() == SYSTEM_AUDIENCE
    return SkillAudience.SYSTEM if is_system else SkillAudience.PEOPLE
