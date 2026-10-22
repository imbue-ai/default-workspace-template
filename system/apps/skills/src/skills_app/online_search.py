"""Finding skills online: the search skills.sh answers, which is what ``npx skills find`` itself asks.

The skills CLI (``npx skills``) installs a skill into the workspace's .agents/skills whichever agents read it, and
records where it came from in a skills lock file; skills.sh is the index it searches. Its search endpoint
(``/api/search?q=&limit=``, the CLI's ``searchSkillsAPI``) answers each match's name, source repository and install
count, most installed first. The page cannot reach another site itself (its content security policy keeps fetches on
its own origin), so this server asks on its behalf, holding each answer for a few minutes: the index changes slowly,
and the page asks again whenever the workspace's skills change, to keep its "Installed" marks current. Nothing is
searched until a person asks to look online, since the query leaves the workspace.
"""

import threading
import time
from collections.abc import Sequence
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from skills_app.data_types import IndexedSkill
from skills_app.data_types import OnlineSkill
from skills_app.data_types import OnlineSkillState
from skills_app.errors import MalformedSkillIndexAnswerError
from skills_app.errors import SkillIndexUnavailableError
from skills_app.interfaces import SkillIndexSearcherInterface
from skills_catalog.data_types import SkillSummary

SKILLS_SH_URL: Final[str] = "https://skills.sh"
SEARCH_PATH: Final[str] = "/api/search"
# As many as the CLI asks for.
SEARCH_RESULT_LIMIT: Final[int] = 20
# The CLI does not search on fewer characters either.
MIN_QUERY_LENGTH: Final[int] = 2
_FETCH_TIMEOUT_SECONDS: Final[float] = 10.0
_FETCH_SLOW_SECONDS: Final[float] = 3.0
_ANSWER_KEPT_SECONDS: Final[float] = 300.0


class SkillsShSearcher(SkillIndexSearcherInterface):
    """Asks skills.sh, one GET per query, keeping each answer for a few minutes."""

    base_url: str = Field(default=SKILLS_SH_URL, frozen=True, description="The index's origin")
    answer_kept_seconds: float = Field(
        default=_ANSWER_KEPT_SECONDS,
        frozen=True,
        description="How long an answer is reused before the index is asked again",
    )
    _answers: dict[str, tuple[float, tuple[IndexedSkill, ...]]] = PrivateAttr(default_factory=dict)
    # The server answers each request on its own thread; the fetch itself runs outside the lock.
    _answers_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def search(self, query: str) -> tuple[IndexedSkill, ...]:
        key = query.strip().lower()
        with self._answers_lock:
            kept = self._answers.get(key)
        if kept is not None and time.monotonic() - kept[0] < self.answer_kept_seconds:
            return kept[1]
        matches = self._fetch(key)
        fetched_at = time.monotonic()
        with self._answers_lock:
            # Searching as the person types asks once per pause, so expired answers are dropped rather than kept for
            # good.
            self._answers = {
                kept_key: answer
                for kept_key, answer in self._answers.items()
                if fetched_at - answer[0] < self.answer_kept_seconds
            }
            self._answers[key] = (fetched_at, matches)
        return matches

    def _fetch(self, query: str) -> tuple[IndexedSkill, ...]:
        url = f"{self.base_url}{SEARCH_PATH}"
        started_at = time.monotonic()
        try:
            response = httpx.get(
                url,
                params={"q": query, "limit": str(SEARCH_RESULT_LIMIT)},
                timeout=_FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        except httpx.HTTPError as e:
            raise SkillIndexUnavailableError(f"Could not search {url} for {query!r}: {e}") from e
        elapsed = time.monotonic() - started_at
        if elapsed > _FETCH_SLOW_SECONDS:
            logger.warning("Searched {} for {!r} slowly, in {:.1f}s", url, query, elapsed)
        if response.is_error:
            raise SkillIndexUnavailableError(f"Searching {url} for {query!r} answered {response.status_code}")
        try:
            answer = response.json()
        except ValueError as e:
            raise MalformedSkillIndexAnswerError(f"Searching {url} for {query!r} answered something not JSON") from e
        return indexed_skills_from_answer(answer)


@pure
def indexed_skills_from_answer(answer: object) -> tuple[IndexedSkill, ...]:
    """The matches in a skills.sh search answer, most installed first; an entry that does not parse is left out.

    Only the fields the page shows are read, so a field skills.sh adds later changes nothing."""
    entries = answer.get("skills") if isinstance(answer, dict) else None
    if not isinstance(entries, list):
        raise MalformedSkillIndexAnswerError("The search answer has no skills list")
    matches: list[IndexedSkill] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            matches.append(
                IndexedSkill(
                    name=entry.get("name"),
                    source=entry.get("source"),
                    installs=entry.get("installs") or 0,
                    page_id=entry.get("id"),
                )
            )
        except ValidationError as e:
            logger.trace("Left out a search result that does not parse: {}", e)
    return tuple(sorted(matches, key=lambda match: match.installs, reverse=True))


@pure
def online_skills(matches: Sequence[IndexedSkill], installed: Sequence[SkillSummary]) -> tuple[OnlineSkill, ...]:
    """Each match as the page shows it, marked against the workspace's own skills.

    A skill installs into a folder named after it, so a match is installed when the workspace has a skill of that name
    recorded as coming from the same source, and its name is taken when the workspace has a different skill of that
    name: installing it would land on that one.
    """
    by_name = {skill.name.lower(): skill for skill in installed}
    return tuple(
        OnlineSkill(
            name=match.name,
            source=match.source,
            installs=match.installs,
            page_url=f"{SKILLS_SH_URL}/{match.page_id}",
            install_command=f"npx skills add {match.source}@{match.name}",
            state=_state_of(match, by_name.get(match.name.lower())),
        )
        for match in matches
    )


def _state_of(match: IndexedSkill, local: SkillSummary | None) -> OnlineSkillState:
    if local is None:
        return OnlineSkillState.AVAILABLE
    is_same_source = local.install_source is not None and local.install_source.source.lower() == match.source.lower()
    return OnlineSkillState.INSTALLED if is_same_source else OnlineSkillState.NAME_TAKEN
