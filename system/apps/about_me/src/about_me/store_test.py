import json
import threading
from pathlib import Path

import pytest

from about_me.errors import InvalidFactError
from about_me.errors import StaleFactError
from about_me.errors import StaleMemoryError
from about_me.profile import split_lines
from about_me.store import MEMORY_INDEX_PATH
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
    profile = store.forget_lookup()

    assert profile.facts == ()
    assert not (workspace / PROFILE_PATH).exists()
    assert not (workspace / SOURCES_PATH).exists()
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == _without_line(SAMPLE_INDEX, 1)


def test_forget_all_leaves_an_index_without_the_line_untouched_and_tolerates_missing_files(
    empty_workspace: Path,
) -> None:
    index = b"- [Other](other.md) -- something else\r\nno newline at the end"
    write_workspace_file(empty_workspace, MEMORY_INDEX_PATH, index)

    ProfileStore(empty_workspace).forget_lookup()

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


def test_editing_a_web_fact_changes_only_its_words_keeping_its_source_and_date(workspace: Path) -> None:
    store = ProfileStore(workspace)
    fact_id = _fact_id_by_text(store, "Writes a newsletter about type")

    profile = store.edit_fact(fact_id, "  Writes a weekly   newsletter about type ")

    expected = SAMPLE_PROFILE.replace(
        b"- Writes a newsletter about type ([Newsletter]",
        b"- Writes a weekly newsletter about type ([Newsletter]",
    )
    assert (workspace / PROFILE_PATH).read_bytes() == expected
    edited = next(fact for fact in profile.facts if fact.text == "Writes a weekly newsletter about type")
    assert (edited.source_url, edited.found_on) == ("https://example.com/news", "2026-11-02")
    assert (workspace / SOURCES_PATH).read_bytes() == SAMPLE_SOURCES


def test_editing_a_told_fact_or_an_unparsed_line_keeps_its_kind(workspace: Path) -> None:
    store = ProfileStore(workspace)

    store.edit_fact(_fact_id_by_text(store, "Is learning Japanese"), "Is learning Korean")
    profile = store.edit_fact(_fact_id_by_text(store, "Something written by hand, with no link"), "Written by hand")

    content = (workspace / PROFILE_PATH).read_bytes()
    assert b"- Is learning Korean (you told me) <!-- found 2026-11-03 -->\n" in content
    assert b"- Written by hand\n" in content
    assert [fact.text for fact in profile.facts][-2:] == ["Is learning Korean", "Written by hand"]


def test_an_edit_keeps_a_crlf_line_ending(tmp_path: Path) -> None:
    write_workspace_file(tmp_path, PROFILE_PATH, b"- Old words (you told me) <!-- found 2026-11-03 -->\r\n")
    store = ProfileStore(tmp_path)

    store.edit_fact(_fact_id_by_text(store, "Old words"), "New words")

    assert (tmp_path / PROFILE_PATH).read_bytes() == b"- New words (you told me) <!-- found 2026-11-03 -->\r\n"


def test_a_stale_or_invalid_edit_is_refused_and_changes_nothing(workspace: Path) -> None:
    store = ProfileStore(workspace)
    fact_id = _fact_id_by_text(store, "Is learning Japanese")

    with pytest.raises(InvalidFactError):
        store.edit_fact(fact_id, "  ")
    with pytest.raises(StaleFactError):
        store.edit_fact("0-0000000000000000", "Anything")

    assert (workspace / PROFILE_PATH).read_bytes() == SAMPLE_PROFILE


def _memory(store: ProfileStore, file_name: str):
    return next(memory for memory in store.read().memories if memory.file_name == file_name)


def test_read_lists_every_other_memory_with_its_title_type_and_body(store: ProfileStore) -> None:
    memories = {memory.file_name: memory for memory in store.read().memories}

    assert set(memories) == {"coding-style.md", "projects.md"}
    assert (memories["coding-style.md"].title, memories["coding-style.md"].memory_type) == ("Coding style", "feedback")
    assert memories["projects.md"].body == "A grading helper."


def test_adding_a_memory_writes_a_user_memory_file_and_its_index_line(empty_workspace: Path) -> None:
    store = ProfileStore(empty_workspace)

    profile = store.add_memory("  I teach   recitations for an algorithms class ")

    [memory] = profile.memories
    assert (memory.file_name, memory.memory_type, memory.body) == (
        "user-i-teach-recitations-for-an-algorithms.md",
        "user",
        "I teach recitations for an algorithms class",
    )
    assert (empty_workspace / MEMORY_INDEX_PATH).read_text() == (
        "- [I teach recitations for an algorithms class](user-i-teach-recitations-for-an-algorithms.md)"
        " -- I teach recitations for an algorithms class\n"
    )


def test_adding_the_same_words_twice_makes_two_files(empty_workspace: Path) -> None:
    store = ProfileStore(empty_workspace)
    store.add_memory("Runs on weekends")

    profile = store.add_memory("Runs on weekends")

    assert sorted(memory.file_name for memory in profile.memories) == [
        "user-runs-on-weekends-2.md",
        "user-runs-on-weekends.md",
    ]


def test_adding_appends_to_an_index_without_a_trailing_newline(workspace: Path) -> None:
    write_workspace_file(workspace, MEMORY_INDEX_PATH, b"- [Projects](projects.md) -- what the user builds")

    ProfileStore(workspace).add_memory("Likes sailing")

    assert (workspace / MEMORY_INDEX_PATH).read_text() == (
        "- [Projects](projects.md) -- what the user builds\n- [Likes sailing](user-likes-sailing.md) -- Likes sailing\n"
    )


def test_editing_a_memory_rewrites_its_description_body_and_index_hook_only(
    store: ProfileStore, workspace: Path
) -> None:
    memory = _memory(store, "coding-style.md")

    store.edit_memory("coding-style.md", memory.revision, "Likes small functions", "Prefers functions under 20 lines.")

    content = (workspace / MEMORY_INDEX_PATH.parent / "coding-style.md").read_text()
    assert content == (
        "---\nname: coding-style\ndescription: Likes small functions\nmetadata:\n  type: feedback\n"
        "  updated: 2026-11-01\n---\n\nPrefers functions under 20 lines.\n"
    )
    assert (
        "- [Coding style](coding-style.md) -- Likes small functions\n" in (workspace / MEMORY_INDEX_PATH).read_text()
    )


def test_deleting_a_memory_removes_the_file_and_its_index_line(store: ProfileStore, workspace: Path) -> None:
    memory = _memory(store, "projects.md")

    profile = store.delete_memory("projects.md", memory.revision)

    assert "projects.md" not in {memory.file_name for memory in profile.memories}
    assert not (workspace / MEMORY_INDEX_PATH.parent / "projects.md").exists()
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == _without_line(SAMPLE_INDEX, 2)


def test_deleting_the_lookups_memory_forgets_the_lookup(store: ProfileStore, workspace: Path) -> None:
    revision = store.read().profile_revision
    assert revision is not None

    store.delete_memory("user-public-profile.md", revision)

    assert not (workspace / PROFILE_PATH).exists()
    assert not (workspace / SOURCES_PATH).exists()


def test_a_stale_or_foreign_memory_is_refused_and_changes_nothing(store: ProfileStore, workspace: Path) -> None:
    memory = _memory(store, "projects.md")

    with pytest.raises(StaleMemoryError):
        store.delete_memory("projects.md", "0000000000000000")
    with pytest.raises(StaleMemoryError):
        store.edit_memory("../projects.md", memory.revision, "x", "y")
    with pytest.raises(StaleMemoryError):
        store.delete_memory("MEMORY.md", "0000000000000000")
    with pytest.raises(InvalidFactError):
        store.edit_memory("projects.md", memory.revision, " ", "y")

    assert (workspace / MEMORY_INDEX_PATH.parent / "projects.md").exists()
    assert (workspace / MEMORY_INDEX_PATH).read_bytes() == SAMPLE_INDEX


def test_forget_everything_removes_every_memory_the_index_and_the_sources(
    store: ProfileStore, workspace: Path
) -> None:
    (workspace / MEMORY_INDEX_PATH.parent / "notes.txt").write_text("not a memory")

    profile = store.forget_everything()

    assert (profile.memories, profile.facts) == ((), ())
    assert sorted(path.name for path in (workspace / MEMORY_INDEX_PATH.parent).iterdir()) == ["notes.txt"]
    assert not (workspace / SOURCES_PATH).exists()


def test_the_folders_readme_is_not_a_memory_and_survives_forgetting_everything(
    store: ProfileStore, workspace: Path
) -> None:
    readme = workspace / MEMORY_INDEX_PATH.parent / "README.md"
    readme.write_text("# data/memories/\n")

    assert "README.md" not in {memory.file_name for memory in store.read().memories}
    store.forget_everything()
    assert readme.exists()


def test_a_new_memory_file_is_readable_by_all(empty_workspace: Path) -> None:
    profile = ProfileStore(empty_workspace).add_memory("Likes sailing")

    path = empty_workspace / MEMORY_INDEX_PATH.parent / profile.memories[0].file_name
    assert path.stat().st_mode & 0o777 == 0o644
