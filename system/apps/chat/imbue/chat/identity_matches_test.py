"""Tests for the identity-match store: filing, superseding, answering, and what survives a restart."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.chat.identity_matches import IdentityCandidate
from imbue.chat.identity_matches import IdentityMatchNotPendingError
from imbue.chat.identity_matches import IdentityMatchStatus
from imbue.chat.identity_matches import IdentityMatchStore
from imbue.chat.identity_matches import InvalidIdentityMatchError
from imbue.chat.identity_matches import REJECTION_MESSAGE
from imbue.chat.identity_matches import UnknownIdentityMatchError
from imbue.chat.identity_matches import confirmation_message

_CHAT = "agent-00000000000000000000000000000001"
_OTHER_CHAT = "agent-00000000000000000000000000000002"


def _candidate(name: str = "Ada Lovelace") -> IdentityCandidate:
    return IdentityCandidate(
        name=name,
        summary="Mathematician at the Analytical Engine project",
        source_url="https://example.org/ada",
        source_title="Ada Lovelace - Example",
    )


def _store(tmp_path: Path) -> IdentityMatchStore:
    return IdentityMatchStore(matches_directory=tmp_path / "identity-matches")


def test_a_filed_match_is_pending_and_listed(tmp_path: Path) -> None:
    store = _store(tmp_path)

    filed = store.file_match(_CHAT, "  Ada Lovelace, London  ", [_candidate(), _candidate("Ada King")])

    assert filed.match.match_id.startswith("identity-")
    assert filed.match.status is IdentityMatchStatus.PENDING
    assert filed.match.searched_for == "Ada Lovelace, London"
    assert [candidate.name for candidate in filed.match.candidates] == ["Ada Lovelace", "Ada King"]
    assert filed.superseded == ()
    assert store.list_pending() == (filed.match,)


def test_a_new_match_supersedes_the_pending_one_in_the_same_chat_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.file_match(_CHAT, "Ada", [_candidate()]).match
    other_chat = store.file_match(_OTHER_CHAT, "Ada", [_candidate()]).match

    second = store.file_match(_CHAT, "Ada Lovelace", [_candidate()])

    (superseded,) = second.superseded
    assert superseded.match_id == first.match_id
    assert superseded.status is IdentityMatchStatus.SUPERSEDED
    assert superseded.superseded_by == second.match.match_id
    assert {match.match_id for match in store.list_pending()} == {other_chat.match_id, second.match.match_id}


def test_confirm_records_the_pick_and_the_message_names_it_with_its_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_match(_CHAT, "Ada", [_candidate(), _candidate("Ada King")]).match

    confirmed = store.confirm(filed.match_id, 1)

    assert confirmed.status is IdentityMatchStatus.CONFIRMED
    assert confirmed.chosen_index == 1
    assert confirmed.resolved_at is not None
    assert confirmation_message(confirmed) == (
        "That's me: Ada King, Mathematician at the Analytical Engine project (https://example.org/ada)"
    )
    assert store.list_pending() == ()


def test_reject_closes_the_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_match(_CHAT, "Ada", [_candidate()]).match

    rejected = store.reject(filed.match_id)

    assert rejected.status is IdentityMatchStatus.REJECTED
    assert rejected.chosen_index is None
    assert REJECTION_MESSAGE == "None of those are me."


def test_an_answered_match_cannot_be_answered_again(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_match(_CHAT, "Ada", [_candidate()]).match
    store.reject(filed.match_id)

    with pytest.raises(IdentityMatchNotPendingError):
        store.confirm(filed.match_id, 0)
    with pytest.raises(IdentityMatchNotPendingError):
        store.reject(filed.match_id)


def test_confirm_refuses_an_index_with_no_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_match(_CHAT, "Ada", [_candidate()]).match

    with pytest.raises(InvalidIdentityMatchError):
        store.confirm(filed.match_id, 1)
    assert store.get(filed.match_id) == filed


def test_unknown_and_malformed_ids_are_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.get("identity-ffffffffffffffffffffffffffffffff") is None
    assert store.get("../../etc/passwd") is None
    with pytest.raises(UnknownIdentityMatchError):
        store.reject("identity-ffffffffffffffffffffffffffffffff")


@pytest.mark.parametrize(
    ("searched_for", "candidate_count"),
    [("Ada", 0), ("Ada", 4), ("   ", 1), ("x" * 301, 1)],
)
def test_filing_refuses_malformed_matches(tmp_path: Path, searched_for: str, candidate_count: int) -> None:
    store = _store(tmp_path)

    with pytest.raises(InvalidIdentityMatchError):
        store.file_match(_CHAT, searched_for, [_candidate() for _ in range(candidate_count)])
    assert store.list_pending() == ()


@pytest.mark.parametrize("source_url", ["javascript:alert(1)", "file:///etc/passwd", "example.org/ada", "https://"])
def test_a_candidate_links_only_to_a_web_page(source_url: str) -> None:
    with pytest.raises(ValidationError):
        IdentityCandidate(name="Ada", summary="Mathematician", source_url=source_url, source_title="Ada")


def test_pending_matches_survive_a_new_store_over_the_same_directory(tmp_path: Path) -> None:
    filed = _store(tmp_path).file_match(_CHAT, "Ada", [_candidate()]).match

    assert _store(tmp_path).list_pending() == (filed,)


def test_an_unreadable_record_is_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    filed = store.file_match(_CHAT, "Ada", [_candidate()]).match
    (tmp_path / "identity-matches" / "identity-00000000000000000000000000000000.json").write_text("not json")

    assert store.list_pending() == (filed,)
