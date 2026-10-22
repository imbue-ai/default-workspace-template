from pydantic import Field

from skills_app.data_types import IndexedSkill
from skills_app.errors import SkillIndexUnavailableError
from skills_app.interfaces import SkillIndexSearcherInterface


class RecordingSkillIndexSearcher(SkillIndexSearcherInterface):
    """A skills index that answers every search with the same matches, and keeps every query."""

    matches: tuple[IndexedSkill, ...] = Field(default=(), description="What every search answers")
    is_unreachable: bool = Field(default=False, description="Whether every search fails as the network would")
    queries: list[str] = Field(default_factory=list, description="Every query searched, in order")

    def search(self, query: str) -> tuple[IndexedSkill, ...]:
        self.queries.append(query)
        if self.is_unreachable:
            raise SkillIndexUnavailableError("The skills index is unreachable")
        return self.matches
