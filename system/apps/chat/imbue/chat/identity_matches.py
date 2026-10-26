"""The identity-match store: who a web look-up found for the user, and which one they said was them.

The onboarding agent's look-up (``.agents/shared/onboarding-agent/scripts/lookup_user.py``) searches
the public web for the user in the background and files what it found here through
``POST /api/identity-matches``. The chat page shows the candidates in an "Is this you?" popup; the
user's pick, or "None of these", reaches the agent as an ordinary message in the user's words. Nothing
found on the web is used until the user has picked it.

Matches persist one JSON file each under ``identity-matches/`` in the chat's data directory
(``data/.apps/chat/`` for the live chat), so a chat app restart keeps a pending popup. A newer match
for the same chat supersedes a pending one: only the newest popup is shown.
"""

import os
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import ValidationError

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import RandomId
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

IDENTITY_MATCHES_DIRNAME: Final[str] = "identity-matches"
MAX_CANDIDATES: Final[int] = 3
MAX_NAME_LENGTH: Final[int] = 200
MAX_SUMMARY_LENGTH: Final[int] = 300
MAX_SOURCE_TITLE_LENGTH: Final[int] = 200
MAX_SOURCE_URL_LENGTH: Final[int] = 2000
MAX_SEARCHED_FOR_LENGTH: Final[int] = 300
# Only a web page the user can open: never a javascript:, data: or file: link.
SOURCE_URL_PATTERN: Final[str] = r"^https?://\S+$"


class IdentityMatchError(Exception):
    """Base class for everything the identity-match store refuses or cannot do."""


class InvalidIdentityMatchError(IdentityMatchError, ValueError):
    """Raised when a match's candidates or search words are malformed."""


class UnknownIdentityMatchError(IdentityMatchError, LookupError):
    """Raised when no match has the given id."""

    def __init__(self, match_id: str) -> None:
        self.match_id = match_id
        super().__init__(f"No identity match with id {match_id!r}")


class IdentityMatchNotPendingError(IdentityMatchError, ValueError):
    """Raised when a confirm or reject reaches a match that has already been answered."""

    def __init__(self, match_id: str, status: "IdentityMatchStatus") -> None:
        self.match_id = match_id
        self.status = status
        super().__init__(f"Identity match {match_id!r} is {status.value}, not pending")


class IdentityMatchId(RandomId):
    """Unique identifier for one identity match; the popup and its answer correlate on it."""

    PREFIX = "identity"


class IdentityMatchStatus(LowerCaseStrEnum):
    """Where a match stands; the wire spelling the popup reads."""

    PENDING = auto()
    CONFIRMED = auto()
    REJECTED = auto()
    SUPERSEDED = auto()


class IdentityCandidate(FrozenModel):
    """One person the look-up found, with the page that says so."""

    name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH, description="The person's name as the page gives it")
    summary: str = Field(
        min_length=1, max_length=MAX_SUMMARY_LENGTH, description="One line on who they are: role, place, work"
    )
    source_url: str = Field(
        max_length=MAX_SOURCE_URL_LENGTH, pattern=SOURCE_URL_PATTERN, description="The public page it came from"
    )
    source_title: str = Field(
        min_length=1, max_length=MAX_SOURCE_TITLE_LENGTH, description="That page's title, shown as the source"
    )


class IdentityMatch(FrozenModel):
    """One filed look-up result, as the store keeps it and the popup reads it."""

    match_id: IdentityMatchId = Field(description="The id the popup and its answer share")
    chat_id: str = Field(description="The chat whose agent ran the look-up, and which gets the answer")
    searched_for: str = Field(description="What was searched for, in the user's words: their name and detail")
    candidates: tuple[IdentityCandidate, ...] = Field(description="Who the look-up found, best match first")
    status: IdentityMatchStatus = Field(description="Pending until the user answers or a newer match supersedes it")
    filed_at: datetime = Field(description="When the match was filed")
    resolved_at: datetime | None = Field(default=None, description="When it left the pending state")
    chosen_index: int | None = Field(default=None, description="The candidate the user confirmed, by position")
    superseded_by: IdentityMatchId | None = Field(default=None, description="The newer match for the same chat")


class FiledIdentityMatch(FrozenModel):
    """The filing's outcome: the new match, and the pending ones it closed."""

    match: IdentityMatch = Field(description="The match as filed")
    superseded: tuple[IdentityMatch, ...] = Field(description="Pending matches for the same chat that this one closed")


@pure
def confirmation_message(match: IdentityMatch) -> str:
    """What the user says to the agent when they pick a candidate: who, and the page that shows it."""
    assert match.chosen_index is not None, "only a confirmed match has a chosen candidate"
    candidate = match.candidates[match.chosen_index]
    return f"That's me: {candidate.name}, {candidate.summary} ({candidate.source_url})"


REJECTION_MESSAGE: Final[str] = "None of those are me."


class IdentityMatchStore(MutableModel):
    """The matches on disk; the one writer of them."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    matches_directory: Path = Field(frozen=True, description="One JSON file per match lives here")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def file_match(
        self, chat_id: str, searched_for: str, candidates: Sequence[IdentityCandidate]
    ) -> FiledIdentityMatch:
        """Record a new pending match, superseding any pending one for the same chat.

        Raises InvalidIdentityMatchError when there are no candidates, too many, or the search words are empty.
        """
        if not candidates:
            raise InvalidIdentityMatchError("at least one candidate is required")
        if len(candidates) > MAX_CANDIDATES:
            raise InvalidIdentityMatchError(f"at most {MAX_CANDIDATES} candidates per match")
        if not searched_for.strip():
            raise InvalidIdentityMatchError("what was searched for is required")
        if len(searched_for) > MAX_SEARCHED_FOR_LENGTH:
            raise InvalidIdentityMatchError(
                f"what was searched for is longer than {MAX_SEARCHED_FOR_LENGTH} characters"
            )
        now = _utc_now()
        match = IdentityMatch(
            match_id=IdentityMatchId(),
            chat_id=chat_id,
            searched_for=searched_for.strip(),
            candidates=tuple(candidates),
            status=IdentityMatchStatus.PENDING,
            filed_at=now,
        )
        with self._lock:
            superseded = tuple(
                self._write(
                    pending.model_copy_update(
                        to_update(pending.field_ref().status, IdentityMatchStatus.SUPERSEDED),
                        to_update(pending.field_ref().superseded_by, match.match_id),
                        to_update(pending.field_ref().resolved_at, now),
                    )
                )
                for pending in self._load_all()
                if pending.status is IdentityMatchStatus.PENDING and pending.chat_id == chat_id
            )
            self._write(match)
        logger.debug("Filed identity match {} for chat {} ({} candidates)", match.match_id, chat_id, len(candidates))
        return FiledIdentityMatch(match=match, superseded=superseded)

    def get(self, match_id: str) -> IdentityMatch | None:
        with self._lock:
            return self._read(match_id)

    def list_pending(self) -> tuple[IdentityMatch, ...]:
        """Every match still waiting for an answer, oldest first: what a page that just connected must show."""
        with self._lock:
            pending = [match for match in self._load_all() if match.status is IdentityMatchStatus.PENDING]
        return tuple(sorted(pending, key=lambda match: match.filed_at))

    def confirm(self, match_id: str, candidate_index: int) -> IdentityMatch:
        """Mark the match confirmed on the candidate the user picked.

        Raises UnknownIdentityMatchError, IdentityMatchNotPendingError, or InvalidIdentityMatchError for an index
        that names no candidate.
        """
        with self._lock:
            match = self._pending(match_id)
            if not 0 <= candidate_index < len(match.candidates):
                raise InvalidIdentityMatchError(
                    f"candidate {candidate_index} does not exist; the match has {len(match.candidates)}"
                )
            confirmed = match.model_copy_update(
                to_update(match.field_ref().status, IdentityMatchStatus.CONFIRMED),
                to_update(match.field_ref().chosen_index, candidate_index),
                to_update(match.field_ref().resolved_at, _utc_now()),
            )
            self._write(confirmed)
        logger.debug("Confirmed identity match {} on candidate {}", match_id, candidate_index)
        return confirmed

    def reject(self, match_id: str) -> IdentityMatch:
        """Mark the match rejected: none of the candidates is the user.

        Raises UnknownIdentityMatchError or IdentityMatchNotPendingError.
        """
        with self._lock:
            match = self._pending(match_id)
            rejected = match.model_copy_update(
                to_update(match.field_ref().status, IdentityMatchStatus.REJECTED),
                to_update(match.field_ref().resolved_at, _utc_now()),
            )
            self._write(rejected)
        logger.debug("Rejected identity match {}", match_id)
        return rejected

    def _pending(self, match_id: str) -> IdentityMatch:
        match = self._read(match_id)
        if match is None:
            raise UnknownIdentityMatchError(match_id)
        if match.status is not IdentityMatchStatus.PENDING:
            raise IdentityMatchNotPendingError(match_id, match.status)
        return match

    def _match_path(self, match_id: str) -> Path:
        return self.matches_directory / f"{match_id}.json"

    def _read(self, match_id: str) -> IdentityMatch | None:
        try:
            IdentityMatchId(match_id)
        except ValueError:
            return None
        path = self._match_path(match_id)
        if not path.is_file():
            return None
        return self._load(path)

    def _load(self, path: Path) -> IdentityMatch | None:
        try:
            return IdentityMatch.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as e:
            logger.warning("Skipping an unreadable identity match record {}: {}", path.name, e)
            return None

    def _load_all(self) -> list[IdentityMatch]:
        if not self.matches_directory.is_dir():
            return []
        loaded = (self._load(path) for path in sorted(self.matches_directory.glob("*.json")))
        return [match for match in loaded if match is not None]

    def _write(self, match: IdentityMatch) -> IdentityMatch:
        self.matches_directory.mkdir(parents=True, exist_ok=True)
        path = self._match_path(match.match_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(match.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return match


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
