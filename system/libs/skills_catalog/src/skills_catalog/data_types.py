from datetime import datetime
from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from skills_catalog.primitives import SkillAudience
from skills_catalog.primitives import SkillName
from skills_catalog.primitives import SkillSource


class SkillsHistory(FrozenModel):
    """When a commit last changed each skill directory the history has touched."""

    last_changed_at_by_skill_name: dict[str, datetime] = Field(
        description="The newest commit's time per skill directory name"
    )


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
    author: str = Field(description="The frontmatter's metadata author; empty when it names none")
    source: SkillSource = Field(description="Who it comes from")
    audience: SkillAudience = Field(description="Who it is for: people, or the agents themselves")
    install_source: InstallSource | None = Field(description="What a skills lock file records for it, if anything")
    last_updated_at: datetime = Field(
        description="When it last changed: its newest file's time while it has unsaved changes, else its newest commit's"
    )


class SkillDetail(FrozenModel):
    """One skill as its page shows it."""

    summary: SkillSummary = Field(description="What the list shows")
    directory: Path = Field(description="Its folder, as an absolute path")
    content: str = Field(description="Its SKILL.md exactly as written, frontmatter included")


class SkillListing(FrozenModel):
    """Every skill under .agents/skills, by name."""

    skills_directory: Path = Field(description="The .agents/skills folder, as an absolute path")
    skills: tuple[SkillSummary, ...] = Field(description="One per skill directory holding a SKILL.md")
