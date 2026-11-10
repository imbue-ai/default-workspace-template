import pytest

from about_me.memories import humanized
from about_me.memories import index_titles
from about_me.memories import is_memory_file_name
from about_me.memories import parse_memory
from about_me.memories import render_memory
from about_me.memories import with_description
from about_me.memories import without_index_lines
from about_me.memories import yaml_scalar
from about_me.testing import SAMPLE_FEEDBACK_MEMORY
from about_me.testing import SAMPLE_INDEX
from about_me.testing import SAMPLE_PROJECT_MEMORY


def test_a_memory_reads_its_name_description_nested_type_and_body() -> None:
    parsed = parse_memory(SAMPLE_FEEDBACK_MEMORY.decode())

    assert (parsed.name, parsed.description, parsed.memory_type) == (
        "coding-style",
        "How the user likes code",
        "feedback",
    )
    assert parsed.body == "Prefers small functions.\n**Why:** they review on a phone."


def test_a_top_level_type_and_a_quoted_description_are_read() -> None:
    parsed = parse_memory(SAMPLE_PROJECT_MEMORY.decode())

    assert (parsed.description, parsed.memory_type) == ("What the user is building", "project")


def test_a_file_without_frontmatter_is_all_body() -> None:
    parsed = parse_memory("Just a note.\n")

    assert (parsed.frontmatter, parsed.name, parsed.memory_type, parsed.body) == ([], None, None, "Just a note.\n")


def test_rendering_keeps_every_frontmatter_line_and_replaces_only_the_description() -> None:
    parsed = parse_memory(SAMPLE_FEEDBACK_MEMORY.decode())

    rendered = render_memory(with_description(parsed.frontmatter, "Likes: small functions"), "New body")

    assert rendered == (
        '---\nname: coding-style\ndescription: "Likes: small functions"\nmetadata:\n  type: feedback\n'
        "  updated: 2026-11-01\n---\n\nNew body\n"
    )
    assert parse_memory(rendered).description == "Likes: small functions"


def test_a_missing_description_is_added_after_the_name() -> None:
    assert with_description(["name: a", "type: user"], "Summary") == ["name: a", "description: Summary", "type: user"]


@pytest.mark.parametrize("value", ["plain words", "a: colon", "#hash", "'quoted'", " padded", "- dash"])
def test_a_scalar_reads_back_as_written(value: str) -> None:
    parsed = parse_memory(render_memory([f"description: {yaml_scalar(value)}"], "body"))

    assert parsed.description == value


def test_index_titles_and_removing_a_files_lines() -> None:
    index = SAMPLE_INDEX.decode()

    assert index_titles(index)["coding-style.md"] == "Coding style"
    assert "coding-style.md" not in without_index_lines(index, "coding-style.md")
    assert without_index_lines(index, "coding-style.md").count("\n") == index.count("\n") - 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [("coding-style.md", True), ("MEMORY.md", False), ("../x.md", False), ("a/b.md", False), ("notes.txt", False)],
)
def test_only_plain_markdown_names_other_than_the_index_are_memory_files(name: str, expected: bool) -> None:
    assert is_memory_file_name(name) is expected


def test_humanized_turns_a_slug_into_words() -> None:
    assert humanized("prefers-short_answers.md") == "Prefers short answers"
