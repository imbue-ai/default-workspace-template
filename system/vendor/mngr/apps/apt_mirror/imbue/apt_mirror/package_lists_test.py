from pathlib import Path

import pytest

from imbue.apt_mirror.errors import AptMirrorPackageListError
from imbue.apt_mirror.package_lists import parse_package_list_text
from imbue.apt_mirror.package_lists import read_package_lists


def test_parse_package_list_text_strips_comments_and_blanks() -> None:
    text = "# header\n\nfoo\nbar# trailing comment\n  baz\n#only comment\n"
    assert parse_package_list_text(text) == ["foo", "bar", "baz"]


def test_read_package_lists_deduplicates_across_files_preserving_order(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    first.write_text("foo\nbar\n")
    second = tmp_path / "b.txt"
    second.write_text("bar\nbaz\n")
    assert read_package_lists([first, second]) == ["foo", "bar", "baz"]


def test_read_package_lists_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AptMirrorPackageListError):
        read_package_lists([tmp_path / "nope.txt"])
