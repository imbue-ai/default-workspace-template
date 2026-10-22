from enum import auto
from pathlib import Path
from typing import Final
from typing import Self

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.primitives import NonEmptyStr
from skills_catalog.errors import InvalidSkillNameError

SKILLS_DIR: Final[Path] = Path(".agents/skills")
SKILL_FILENAME: Final[str] = "SKILL.md"
# Where the `skills` CLI (`npx skills add`) records what it installed: the template keeps its lock beside the
# skills, and the CLI writes a new one at the checkout's root.
LOCK_FILE_PATHS: Final[tuple[Path, ...]] = (Path(".agents/skills-lock.json"), Path("skills-lock.json"))
# The frontmatter author every skill that ships with the template carries (``metadata.author``).
STUDIO_AUTHOR: Final[str] = "imbue"
# The frontmatter audience (``metadata.audience``) that marks a skill as the agents' own procedure rather than one a
# person would run or look for; a skill without one is for people.
SYSTEM_AUDIENCE: Final[str] = "system"


class SkillName(NonEmptyStr):
    """A skill's directory name under .agents/skills: one path component, never hidden."""

    def __new__(cls, value: str) -> Self:
        is_one_visible_component = bool(value) and "/" not in value and "\\" not in value and not value.startswith(".")
        if not is_one_visible_component:
            raise InvalidSkillNameError(f"{value!r} is not a skill name")
        return super().__new__(cls, value)


class SkillSource(UpperCaseStrEnum):
    """Who a skill comes from, judged from its frontmatter author and the skills lock files."""

    # Its author is Imbue: it ships with the template, whether or not a lock file also lists it.
    STUDIO = auto()
    # A skills lock file records the repository it was installed from.
    INSTALLED = auto()
    # Anything else: made by someone in this workspace, whatever its author says.
    WORKSPACE = auto()


class SkillAudience(UpperCaseStrEnum):
    """Who a skill is for, from its frontmatter's ``metadata.audience``."""

    # One a person would run by name or want to know of: every skill that does not say otherwise.
    PEOPLE = auto()
    # The agents' own procedure (``metadata.audience: system``), which the Skills app keeps behind the scenes.
    SYSTEM = auto()
