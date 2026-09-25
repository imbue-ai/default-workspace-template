"""Unit tests for the shared label helpers.

These sit here rather than under a harness because every harness's labeller calls
them, so the answers must not be able to differ between them -- the same reason
``tool_output_test`` holds the shared tool-output rules.
"""

from imbue.chat.harnesses.tool_labels import basename


def test_a_path_is_named_by_its_last_segment_however_deep_it_is() -> None:
    """The point of naming rather than pathing: the meaningful half is the end, so a
    long path must never be clipped from the front and lose it."""
    assert basename("/a/very/deeply/nested/set/of/folders/final_name.py") == "final_name.py"
    assert basename("relative_file.md") == "relative_file.md"


def test_a_directory_keeps_its_trailing_slash() -> None:
    """Without it a directory reads as a file with no extension. The slash is the only
    thing in a path that says which it is."""
    assert basename("/home/user/workspace/some/dir/") == "dir/"
    assert basename("dir/") == "dir/"


def test_a_directory_written_without_a_slash_is_left_alone() -> None:
    """Inventing one would be a guess: `file_path` and `path` both carry files and
    directories, and nothing in the string tells them apart."""
    assert basename("/home/user/workspace/some/dir") == "dir"


def test_the_root_names_itself_rather_than_doubling_its_slash() -> None:
    assert basename("/") == "/"
