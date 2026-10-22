from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel
from skills_app.data_types import IndexedSkill


class SkillIndexSearcherInterface(MutableModel, ABC):
    """Searches the online skills index."""

    @abstractmethod
    def search(self, query: str) -> tuple[IndexedSkill, ...]:
        """The matches for ``query``, most installed first; raises SkillIndexUnavailableError when it cannot answer."""
