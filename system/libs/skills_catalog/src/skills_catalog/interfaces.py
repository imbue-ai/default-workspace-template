from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel
from skills_catalog.data_types import SkillDetail
from skills_catalog.data_types import SkillListing
from skills_catalog.data_types import SkillsHistory
from skills_catalog.primitives import SkillName


class SkillHistoryReaderInterface(MutableModel, ABC):
    """Reads what the workspace's version history says about the skills directory."""

    @abstractmethod
    def read_history(self) -> SkillsHistory:
        """When a commit last changed each skill directory; raises GitHistoryError when the history cannot be read."""

    @abstractmethod
    def read_names_with_unsaved_changes(self) -> frozenset[str]:
        """The skill directories holding changes no commit has saved; raises GitHistoryError on failure."""


class SkillsCatalogInterface(MutableModel, ABC):
    """Answers what skills the workspace's agents have, read fresh from .agents/skills on every call."""

    @abstractmethod
    def list_skills(self) -> SkillListing:
        """Every skill directory holding a SKILL.md, by name."""

    @abstractmethod
    def describe_skill(self, name: SkillName) -> SkillDetail:
        """One skill with its SKILL.md as written; raises SkillNotFoundError when there is no such skill."""
