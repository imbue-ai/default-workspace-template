from pathlib import Path

import pytest

from imbue.apt_mirror.data_types import PackageSpec
from imbue.apt_mirror.errors import AptMirrorPackageListError
from imbue.apt_mirror.package_lists import parse_package_list_text
from imbue.apt_mirror.package_lists import parse_package_spec
from imbue.apt_mirror.package_lists import read_package_lists


def test_parse_package_list_text_strips_comments_and_blanks() -> None:
    text = "# header\n\nfoo\nbar# trailing comment\n  baz\n#only comment\n"
    assert [spec.text for spec in parse_package_list_text(text)] == ["foo", "bar", "baz"]


def test_parse_package_spec_reads_an_optional_exact_version() -> None:
    assert parse_package_spec("docker-ce") == PackageSpec(name="docker-ce", version=None)
    assert parse_package_spec("docker-ce=5:29.6.2-1~debian.13~trixie") == PackageSpec(
        name="docker-ce", version="5:29.6.2-1~debian.13~trixie"
    )


def test_parse_package_spec_rejects_malformed_entries() -> None:
    for text in ("=1.0", "docker-ce="):
        with pytest.raises(AptMirrorPackageListError):
            parse_package_spec(text)


def test_read_package_lists_deduplicates_across_files_preserving_order(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    first.write_text("foo\nbar\n")
    second = tmp_path / "b.txt"
    second.write_text("bar\nbaz\nfoo=2.0\n")
    assert [spec.text for spec in read_package_lists([first, second])] == ["foo", "bar", "baz", "foo=2.0"]


def test_read_package_lists_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(AptMirrorPackageListError):
        read_package_lists([tmp_path / "nope.txt"])
