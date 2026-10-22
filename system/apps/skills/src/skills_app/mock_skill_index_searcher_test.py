import threading
from typing import Final

from pydantic import Field
from pydantic import PrivateAttr

from skills_app.data_types import IndexedSkill
from skills_app.errors import SkillIndexUnavailableError
from skills_app.interfaces import SkillIndexSearcherInterface

# How long a held search waits to be released before it answers anyway, so a test that forgets cannot hang the server.
_HELD_ANSWER_TIMEOUT_SECONDS: Final[float] = 30.0


def _released() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


class RecordingSkillIndexSearcher(SkillIndexSearcherInterface):
    """A skills index that answers every search with the same matches, and keeps every query."""

    matches: tuple[IndexedSkill, ...] = Field(default=(), description="What every search answers")
    is_unreachable: bool = Field(default=False, description="Whether every search fails as the network would")
    queries: list[str] = Field(default_factory=list, description="Every query searched, in order")
    _answers_released: threading.Event = PrivateAttr(default_factory=_released)

    def hold_answers(self) -> None:
        """Keep every search waiting, as a slow index would, until ``release_answers``."""
        self._answers_released.clear()

    def release_answers(self) -> None:
        self._answers_released.set()

    def search(self, query: str) -> tuple[IndexedSkill, ...]:
        self.queries.append(query)
        self._answers_released.wait(timeout=_HELD_ANSWER_TIMEOUT_SECONDS)
        if self.is_unreachable:
            raise SkillIndexUnavailableError("The skills index is unreachable")
        return self.matches
