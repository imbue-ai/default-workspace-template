import datetime
import json
import threading
from pathlib import Path

import pytest

from about_me.errors import InvalidFactError
from about_me.errors import StaleFactError
from about_me.profile import split_lines
from about_me.store import MAX_FACT_CHARS
from about_me.store import MEMORY_INDEX_PATH
from about_me.store import NEW_INDEX_LINE
from about_me.store import PROFILE_PATH
from about_me.store import ProfileStore
from about_me.store import SOURCES_PATH
from about_me.store import write_atomically
from about_me.testing import SAMPLE_INDEX
from about_me.testing import SAMPLE_PROFILE
from about_me.testing import SAMPLE_SOURCES
from about_me.testing import write_workspace_file


def _without_line(content: bytes, index: int) -> bytes:
    lines = split_lines(content)
    return b"".join(lines[:index] + lines[index + 1 :])


def _fact_id_by_text(store: ProfileStore, text: str) -> str:
    return next(fact.id for fact in store.read().facts if fact.text == text)


def _source_titles(workspace: Path) -> list[str]:
    return [entry["title"] for entry in json.loads((workspace / SOURCES_PATH).read_bytes())]


def test_read_shows_every_fact_and_both_raw_files_as_they_are(store: ProfileStore) -> None:
    profile = store.read()

    assert len(profile.facts) == 5
    assert profile.profile_markdown == SAMPLE_PROFILE.decode()
    assert profile.sources_json == SAMPLE_SOURCES.decode()


def test_read_of_a_workspace_with_no_files_is_empty(empty_workspace: Path) -> None:
    profile = ProfileStore(empty_workspace).read()

    assert profile.facts == ()
    assert profile.profile_markdown is None
    assert profile.sources_json is None


def test_a_sources_path_that_cannot_be_read_reads_as_missing(workspace: Path) -> None:
    (workspace / SOURCES_PATH).unlink()
    (workspace / SOURCES_PATH).mkdir()

    assert ProfileStore(workspace).read().sources_json is None


def test_deleting_a_fact_removes_exactly_its_line(store: ProfileStore, workspace: Path) -> None:
    store.delete_fact(_fact_id_by_text(store, "Writes a newsletter about type"))

    assert (workspace / PROFILE_PATH).read_bytes() == _without_line(SAMPLE_PROFILE, 9)
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == SAMPLE_INDEX


def test_deleting_the_only_fact_citing_a_url_drops_that_source_and_keeps_the_rest(
    store: ProfileStore, workspace: Path
) -> None:
    profile = store.delete_fact(_fact_id_by_text(store, "Writes a newsletter about type"))

    assert _source_titles(workspace) == ["Portfolio", "What you told me", "No url key at all"]
    assert [fact.text for fact in profile.facts] == [
        "Designs typefaces in Berlin",
        "Teaches a weekend lettering class",
        "Is learning Japanese",
        "Something written by hand, with no link",
    ]


def test_deleting_one_of_two_facts_citing_a_url_keeps_the_source_file_untouched(
    store: ProfileStore, workspace: Path
) -> None:
    store.delete_fact(_fact_id_by_text(store, "Designs typefaces in Berlin"))

    assert (workspace / SOURCES_PATH).read_bytes() == SAMPLE_SOURCES


def test_deleting_the_last_fact_the_user_shared_drops_their_null_url_entries(
    store: ProfileStore, workspace: Path
) -> None:
    store.delete_fact(_fact_id_by_text(store, "Is learning Japanese"))

    assert _source_titles(workspace) == ["Portfolio", "Newsletter", "No url key at all"]


def test_deleting_a_shared_fact_keeps_the_user_entries_while_another_shared_fact_remains(workspace: Path) -> None:
    write_workspace_file(
        workspace,
        PROFILE_PATH,
        b"- One (you told me) <!-- found 2026-01-01 -->\n- Two (you told me) <!-- found 2026-01-01 -->\nDone.\n",
    )
    store = ProfileStore(workspace)

    store.delete_fact(_fact_id_by_text(store, "One"))

    assert (workspace / SOURCES_PATH).read_bytes() == SAMPLE_SOURCES


def test_deleting_an_unparsed_line_removes_it_whole(store: ProfileStore, workspace: Path) -> None:
    store.delete_fact(_fact_id_by_text(store, "Something written by hand, with no link"))

    assert (workspace / PROFILE_PATH).read_bytes() == _without_line(SAMPLE_PROFILE, 12)
    assert (workspace / SOURCES_PATH).read_bytes() == SAMPLE_SOURCES


def test_deleting_the_last_fact_forgets_everything(workspace: Path) -> None:
    write_workspace_file(
        workspace,
        PROFILE_PATH,
        b"---\nname: user-public-profile\n---\n- Only ([P](https://example.com/news)) <!-- found 2026-01-01 -->\n"
        b"These facts came from a lookup.\n",
    )
    store = ProfileStore(workspace)

    profile = store.delete_fact(store.read().facts[0].id)

    assert profile.facts == ()
    assert profile.profile_markdown is None
    assert profile.sources_json is None
    assert not (workspace / PROFILE_PATH).exists()
    assert not (workspace / SOURCES_PATH).exists()
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == _without_line(SAMPLE_INDEX, 1)


def test_a_stale_id_is_refused_and_changes_nothing(store: ProfileStore, workspace: Path) -> None:
    stale_id = _fact_id_by_text(store, "Writes a newsletter about type")
    # The agent rewrites the memory: a line above moves the fact down one.
    edited = SAMPLE_PROFILE.replace(b"\n\n- Designs", b"\n\n- A new first fact\n- Designs")
    (workspace / PROFILE_PATH).write_bytes(edited)

    with pytest.raises(StaleFactError):
        store.delete_fact(stale_id)

    assert (workspace / PROFILE_PATH).read_bytes() == edited
    assert (workspace / SOURCES_PATH).read_bytes() == SAMPLE_SOURCES


def test_an_unknown_id_or_a_missing_profile_is_stale(store: ProfileStore, workspace: Path) -> None:
    with pytest.raises(StaleFactError):
        store.delete_fact("0-0000000000000000")
    (workspace / PROFILE_PATH).unlink()
    with pytest.raises(StaleFactError):
        store.delete_fact("8-0000000000000000")


def test_a_corrupt_or_missing_sources_file_does_not_stop_a_delete(store: ProfileStore, workspace: Path) -> None:
    (workspace / SOURCES_PATH).write_bytes(b"{not json")

    store.delete_fact(_fact_id_by_text(store, "Writes a newsletter about type"))

    assert (workspace / SOURCES_PATH).read_bytes() == b"{not json"
    (workspace / SOURCES_PATH).unlink()
    store.delete_fact(_fact_id_by_text(store, "Is learning Japanese"))
    assert not (workspace / SOURCES_PATH).exists()
    assert len(store.read().facts) == 3


def test_a_sources_file_that_is_not_an_array_is_left_alone(store: ProfileStore, workspace: Path) -> None:
    (workspace / SOURCES_PATH).write_bytes(b'{"url": "https://example.com/news"}')

    store.delete_fact(_fact_id_by_text(store, "Writes a newsletter about type"))

    assert (workspace / SOURCES_PATH).read_bytes() == b'{"url": "https://example.com/news"}'


def test_forget_all_removes_the_profile_its_index_line_and_the_sources(store: ProfileStore, workspace: Path) -> None:
    profile = store.forget_all()

    assert profile.facts == ()
    assert not (workspace / PROFILE_PATH).exists()
    assert not (workspace / SOURCES_PATH).exists()
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == _without_line(SAMPLE_INDEX, 1)


def test_forget_all_leaves_an_index_without_the_line_untouched_and_tolerates_missing_files(
    empty_workspace: Path,
) -> None:
    index = b"- [Other](other.md) -- something else\r\nno newline at the end"
    write_workspace_file(empty_workspace, MEMORY_INDEX_PATH, index)

    ProfileStore(empty_workspace).forget_all()

    assert (empty_workspace / MEMORY_INDEX_PATH).read_bytes() == index


def test_write_atomically_replaces_the_file_keeping_its_mode_and_leaves_no_temporary(tmp_path: Path) -> None:
    path = tmp_path / "file.md"
    path.write_bytes(b"old")
    path.chmod(0o600)

    write_atomically(path, b"new")

    assert path.read_bytes() == b"new"
    assert path.stat().st_mode & 0o777 == 0o600
    assert [child.name for child in tmp_path.iterdir()] == ["file.md"]


def test_concurrent_deletes_are_serialized_and_never_delete_the_wrong_line(workspace: Path) -> None:
    store = ProfileStore(workspace)
    newsletter = _fact_id_by_text(store, "Writes a newsletter about type")
    handwritten = _fact_id_by_text(store, "Something written by hand, with no link")
    refusals: list[StaleFactError] = []

    def delete(fact: str) -> None:
        try:
            store.delete_fact(fact)
        except StaleFactError as error:
            refusals.append(error)

    threads = [threading.Thread(target=delete, args=(fact,)) for fact in (newsletter, handwritten)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # The newsletter line sits above the other, so its id holds in either order; the other's holds only when it
    # runs first, and is otherwise refused rather than taken to mean the line that moved into its place.
    remaining = [fact.text for fact in store.read().facts]
    assert len(refusals) <= 1
    assert "Writes a newsletter about type" not in remaining
    assert ("Something written by hand, with no link" in remaining) == (len(refusals) == 1)
    assert len(remaining) == 3 + len(refusals)


_ADDED_ON = datetime.date(2026, 11, 10)


def _store_on(root: Path) -> ProfileStore:
    return ProfileStore(root, today=lambda: _ADDED_ON)


def test_adding_a_fact_puts_one_told_line_after_the_last_fact(workspace: Path) -> None:
    profile = _store_on(workspace).add_fact("  I teach   recitations\nfor algorithms  ")

    line = b"- I teach recitations for algorithms (you told me) <!-- found 2026-11-10 -->\n"
    lines = split_lines(SAMPLE_PROFILE)
    last_fact = max(index for index, raw in enumerate(lines) if raw.startswith(b"- Something written by hand"))
    assert (workspace / PROFILE_PATH).read_bytes() == b"".join(
        lines[: last_fact + 1] + [line] + lines[last_fact + 1 :]
    )
    added = profile.facts[-1]
    assert (added.text, added.is_told_by_user, added.found_on, added.source_url) == (
        "I teach recitations for algorithms",
        True,
        "2026-11-10",
        None,
    )
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == SAMPLE_INDEX
    assert (workspace / SOURCES_PATH).read_bytes() == SAMPLE_SOURCES


def test_adding_a_fact_with_nothing_remembered_creates_the_profile_its_index_line_and_its_source(
    empty_workspace: Path,
) -> None:
    profile = _store_on(empty_workspace).add_fact("Is learning Japanese")

    assert [fact.text for fact in profile.facts] == ["Is learning Japanese"]
    assert profile.facts[0].is_told_by_user
    assert (empty_workspace / PROFILE_PATH).read_bytes().startswith(b"---\nname: user-public-profile\n")
    assert (empty_workspace / MEMORY_INDEX_PATH).read_bytes() == NEW_INDEX_LINE
    assert json.loads((empty_workspace / SOURCES_PATH).read_bytes()) == [
        {"url": None, "title": "What you told me", "fetched_at": "2026-11-10", "origin": "user"}
    ]


def test_adding_a_fact_appends_the_index_line_to_an_index_without_one(empty_workspace: Path) -> None:
    write_workspace_file(empty_workspace, MEMORY_INDEX_PATH, b"- [Projects](projects.md) -- what the user builds")

    _store_on(empty_workspace).add_fact("Is learning Japanese")

    assert (empty_workspace / MEMORY_INDEX_PATH).read_bytes() == (
        b"- [Projects](projects.md) -- what the user builds\n" + NEW_INDEX_LINE
    )


def test_an_added_fact_is_deleted_like_any_other_and_takes_its_source_with_it(empty_workspace: Path) -> None:
    store = _store_on(empty_workspace)
    store.add_fact("Is learning Japanese")
    store.add_fact("Runs on weekends")

    profile = store.delete_fact(_fact_id_by_text(store, "Runs on weekends"))

    assert [fact.text for fact in profile.facts] == ["Is learning Japanese"]
    assert _source_titles(empty_workspace) == ["What you told me"]


@pytest.mark.parametrize("text", ["", "   \n ", "x" * (MAX_FACT_CHARS + 1), "a <!-- note", "ends -->"])
def test_a_fact_that_cannot_be_one_line_is_refused_and_changes_nothing(workspace: Path, text: str) -> None:
    with pytest.raises(InvalidFactError):
        _store_on(workspace).add_fact(text)

    assert (workspace / PROFILE_PATH).read_bytes() == SAMPLE_PROFILE
    assert (workspace / SOURCES_PATH).read_bytes() == SAMPLE_SOURCES
