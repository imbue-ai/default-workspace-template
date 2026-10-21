from datetime import datetime
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from skills_catalog.primitives import SkillName
from skills_catalog.primitives import SkillOrigin


class SkillCommitFacts(FrozenModel):
    """What the git history says about one skill's directory."""

    is_added_by_template: bool = Field(description="Whether the oldest commit touching it came from the template")
    is_last_changed_by_template: bool = Field(
        description="Whether the newest commit touching it came from the template"
    )
    last_changed_at: datetime = Field(description="When the newest commit touching it was made")


class SkillsHistory(FrozenModel):
    """The git facts for every skill directory the history has ever touched, by directory name."""

    facts_by_skill_name: dict[str, SkillCommitFacts] = Field(description="Facts per skill directory name")


class InstallSource(FrozenModel):
    """Where a skills lock file says a skill was installed from."""

    source: str = Field(description="The repository it came from, e.g. owner/repo")
    source_type: str = Field(description="The kind of source, e.g. github")
    skill_path: str = Field(description="The SKILL.md's path inside that repository")
    lock_file: Path = Field(description="The lock file recording it, relative to the checkout")


class SkillSummary(FrozenModel):
    """One skill as the list shows it."""

    name: SkillName = Field(description="Its directory name under .agents/skills, which agents invoke it by")
    title: str = Field(description="The frontmatter's name, or the directory name when it gives none")
    description: str = Field(description="The frontmatter's description; empty when it gives none")
    origin: SkillOrigin = Field(description="Where it came from")
    install_source: InstallSource | None = Field(description="What a skills lock file records for it, if anything")
    last_changed_at: datetime | None = Field(description="When a commit last changed it; null until one has")
    has_unsaved_changes: bool = Field(description="Whether its directory has changes no commit has saved")
    problems: tuple[str, ...] = Field(description="How its SKILL.md departs from the agentskills.io layout")


class SkillFile(FrozenModel):
    """One file inside a skill's directory."""

    path: Path = Field(description="Relative to the skill's directory")
    size_bytes: int = Field(description="Its size")


class SkillDetail(FrozenModel):
    """One skill as its page shows it."""

    summary: SkillSummary = Field(description="What the list shows")
    directory: Path = Field(description="Its folder, as an absolute path")
    skill_file_path: Path = Field(description="Its SKILL.md, relative to the checkout")
    content: str = Field(description="Its SKILL.md exactly as written, frontmatter included")
    files: tuple[SkillFile, ...] = Field(description="Every file in its directory, by path")
    is_file_list_truncated: bool = Field(description="Whether the directory held more files than are listed")


class SkillListing(FrozenModel):
    """Every skill under .agents/skills, by name."""

    skills_directory: Path = Field(description="The .agents/skills folder, as an absolute path")
    skills: tuple[SkillSummary, ...] = Field(description="One per skill directory holding a SKILL.md")
