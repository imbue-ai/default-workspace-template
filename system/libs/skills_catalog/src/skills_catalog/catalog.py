import os
from collections.abc import Mapping
from collections.abc import Set
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.mngr.utils.agent_skills import skill_shape_problems
from imbue.mngr.utils.agent_skills import split_skill_frontmatter
from skills_catalog.data_types import InstallSource
from skills_catalog.data_types import SkillDetail
from skills_catalog.data_types import SkillFile
from skills_catalog.data_types import SkillListing
from skills_catalog.data_types import SkillSummary
from skills_catalog.data_types import SkillsHistory
from skills_catalog.errors import InvalidSkillNameError
from skills_catalog.errors import SkillNotFoundError
from skills_catalog.git_history import GitSkillHistoryReader
from skills_catalog.interfaces import SkillHistoryReaderInterface
from skills_catalog.interfaces import SkillsCatalogInterface
from skills_catalog.primitives import LOCK_FILE_PATHS
from skills_catalog.primitives import SKILLS_DIR
from skills_catalog.primitives import SKILL_FILENAME
from skills_catalog.primitives import SkillName
from skills_catalog.provenance import classify_skill_origin
from skills_catalog.provenance import classify_skill_source
from skills_catalog.provenance import install_sources_from_lock_text
from skills_catalog.provenance import merge_install_sources

# Names another checkout; absent, the repo root is the current directory, which supervisord and the agents set to
# the workspace checkout.
REPO_ROOT_ENV: Final[str] = "SKILLS_CATALOG_REPO_ROOT"
# A skill's page lists at most this many of its files, so a directory holding a vendored tree stays readable.
MAX_LISTED_FILES: Final[int] = 200
_SKIPPED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset({"__pycache__", "node_modules", ".venv"})


class SkillsCatalog(SkillsCatalogInterface):
    """The skills under a checkout's .agents/skills, read from disk on every call."""

    repo_root: Path = Field(frozen=True, description="The workspace checkout")
    history_reader: SkillHistoryReaderInterface = Field(frozen=True, description="The git history's reader")

    def list_skills(self) -> SkillListing:
        history = self.history_reader.read_history()
        unsaved_names = self.history_reader.read_names_with_unsaved_changes()
        install_sources = self._read_install_sources()
        skills_root = self.repo_root / SKILLS_DIR
        skill_directories = (
            sorted(entry for entry in skills_root.iterdir() if entry.is_dir()) if skills_root.is_dir() else []
        )
        summaries = tuple(
            self._summarize(directory, history, unsaved_names, install_sources)
            for directory in skill_directories
            if (directory / SKILL_FILENAME).is_file() and not directory.name.startswith(".")
        )
        return SkillListing(skills_directory=skills_root.absolute(), skills=summaries)

    def describe_skill(self, name: SkillName) -> SkillDetail:
        directory = self.repo_root / SKILLS_DIR / name
        skill_file = directory / SKILL_FILENAME
        if not skill_file.is_file():
            raise SkillNotFoundError(f"There is no skill called {name!r}.")
        summary = self._summarize(
            directory,
            self.history_reader.read_history(),
            self.history_reader.read_names_with_unsaved_changes(),
            self._read_install_sources(),
        )
        files = _list_skill_files(directory)
        return SkillDetail(
            summary=summary,
            directory=directory.absolute(),
            skill_file_path=SKILLS_DIR / name / SKILL_FILENAME,
            content=skill_file.read_text(encoding="utf-8", errors="replace"),
            files=files[:MAX_LISTED_FILES],
            is_file_list_truncated=len(files) > MAX_LISTED_FILES,
        )

    def _summarize(
        self,
        directory: Path,
        history: SkillsHistory,
        unsaved_names: Set[str],
        install_sources: Mapping[str, InstallSource],
    ) -> SkillSummary:
        skill_file = directory / SKILL_FILENAME
        document = split_skill_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
        install_source = install_sources.get(directory.name)
        # The line-by-line frontmatter reader flattens the metadata map, so its author is a field of its own.
        author = document.fields.get("author", "")
        facts = history.facts_by_skill_name.get(directory.name)
        has_unsaved_changes = directory.name in unsaved_names
        return SkillSummary(
            name=SkillName(directory.name),
            title=document.fields.get("name") or directory.name,
            description=document.fields.get("description", ""),
            author=author,
            source=classify_skill_source(author, install_source is not None),
            origin=classify_skill_origin(facts, install_source is not None),
            install_source=install_source,
            last_changed_at=facts.last_changed_at if facts is not None else None,
            last_updated_at=(
                facts.last_changed_at
                if facts is not None and not has_unsaved_changes
                else _newest_file_time(directory)
            ),
            has_unsaved_changes=has_unsaved_changes,
            problems=skill_shape_problems(skill_file),
        )

    def _read_install_sources(self) -> dict[str, InstallSource]:
        sources_by_lock_file: dict[Path, dict[str, InstallSource]] = {}
        for lock_file in LOCK_FILE_PATHS:
            try:
                lock_text = (self.repo_root / lock_file).read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.trace("No skills lock file at {}", lock_file)
                continue
            sources_by_lock_file[lock_file] = install_sources_from_lock_text(lock_text, lock_file)
        return merge_install_sources(sources_by_lock_file)


def _list_skill_files(directory: Path) -> list[SkillFile]:
    """Every file under the directory by relative path, skipping caches and installed dependencies."""
    files: list[SkillFile] = []
    for current_root, directory_names, file_names in os.walk(directory):
        directory_names[:] = sorted(name for name in directory_names if name not in _SKIPPED_DIRECTORY_NAMES)
        for file_name in sorted(file_names):
            path = Path(current_root) / file_name
            files.append(SkillFile(path=path.relative_to(directory), size_bytes=path.lstat().st_size))
    return files


def _newest_file_time(directory: Path) -> datetime:
    """When the most recently modified file under the directory was modified, caches and dependencies skipped."""
    newest = directory.lstat().st_mtime
    for current_root, directory_names, file_names in os.walk(directory):
        directory_names[:] = [name for name in directory_names if name not in _SKIPPED_DIRECTORY_NAMES]
        for file_name in file_names:
            newest = max(newest, (Path(current_root) / file_name).lstat().st_mtime)
    return datetime.fromtimestamp(newest, tz=UTC)


def parse_skill_name(raw_name: str) -> SkillName | None:
    """The skill name a request names, or None when it cannot name a skill."""
    try:
        return SkillName(raw_name)
    except InvalidSkillNameError:
        return None


def build_catalog(repo_root: Path) -> SkillsCatalog:
    return SkillsCatalog(repo_root=repo_root, history_reader=GitSkillHistoryReader(repo_root=repo_root))


def repo_root_from_environment(environment: Mapping[str, str]) -> Path:
    configured = environment.get(REPO_ROOT_ENV, "")
    return Path(configured) if configured else Path.cwd()
