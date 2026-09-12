import lzma

import pytest

from imbue.apt_mirror.data_types import PackagesIndexEntry
from imbue.apt_mirror.data_types import ReleaseFileEntry
from imbue.apt_mirror.errors import AptMirrorInvalidTimestampError
from imbue.apt_mirror.errors import AptMirrorUnsafePathError
from imbue.apt_mirror.parsing import artifact_object_key
from imbue.apt_mirror.parsing import artifact_url
from imbue.apt_mirror.parsing import by_hash_path_for_entry
from imbue.apt_mirror.parsing import filter_index_entries_for_architectures
from imbue.apt_mirror.parsing import filter_index_entries_for_components
from imbue.apt_mirror.parsing import package_file_object_key
from imbue.apt_mirror.parsing import parse_packages_index_entries
from imbue.apt_mirror.parsing import parse_release_sha256_entries
from imbue.apt_mirror.parsing import select_newest_entry
from imbue.apt_mirror.parsing import snapshot_archive_url
from imbue.apt_mirror.parsing import validate_safe_subpath
from imbue.apt_mirror.parsing import validate_snapshot_timestamp

TIMESTAMP = "20260725T000000Z"


def test_parse_release_sha256_entries_reads_only_sha256_section() -> None:
    release_text = (
        "Suite: trixie\n"
        "MD5Sum:\n"
        " aaaa 100 main/binary-amd64/Packages\n"
        "SHA256:\n"
        " deadbeef 123 main/binary-amd64/Packages.xz\n"
        " cafebabe 456 main/Contents-amd64.gz\n"
        "Description: Debian\n"
    )
    entries = parse_release_sha256_entries(release_text)
    assert entries == [
        ReleaseFileEntry(path="main/binary-amd64/Packages.xz", sha256="deadbeef", size=123),
        ReleaseFileEntry(path="main/Contents-amd64.gz", sha256="cafebabe", size=456),
    ]


def test_filter_index_entries_keeps_wanted_arches_and_arch_independent_files() -> None:
    entries = [
        ReleaseFileEntry(path="main/binary-amd64/Packages.xz", sha256="a", size=1),
        ReleaseFileEntry(path="main/binary-arm64/Packages.xz", sha256="b", size=1),
        ReleaseFileEntry(path="main/binary-all/Packages.xz", sha256="c", size=1),
        ReleaseFileEntry(path="main/binary-i386/Packages.xz", sha256="d", size=1),
        ReleaseFileEntry(path="main/i18n/Translation-en.xz", sha256="e", size=1),
        ReleaseFileEntry(path="main/source/Sources.xz", sha256="f", size=1),
        ReleaseFileEntry(path="main/Contents-amd64.gz", sha256="g", size=1),
        ReleaseFileEntry(path="main/Contents-i386.gz", sha256="h", size=1),
        ReleaseFileEntry(path="main/binary-amd64/Packages.diff/Index", sha256="i", size=1),
        ReleaseFileEntry(path="main/debian-installer/binary-amd64/Packages.xz", sha256="j", size=1),
    ]
    filtered_paths = [e.path for e in filter_index_entries_for_architectures(entries, ("amd64", "arm64"))]
    assert filtered_paths == [
        "main/binary-amd64/Packages.xz",
        "main/binary-arm64/Packages.xz",
        "main/binary-all/Packages.xz",
        "main/i18n/Translation-en.xz",
        "main/Contents-amd64.gz",
    ]


def test_by_hash_path_sits_beside_the_named_index() -> None:
    entry = ReleaseFileEntry(path="main/binary-amd64/Packages.xz", sha256="deadbeef", size=1)
    assert by_hash_path_for_entry(entry) == "main/binary-amd64/by-hash/SHA256/deadbeef"


def test_parse_packages_index_entries_decompresses_by_suffix_and_skips_incomplete_stanzas() -> None:
    packages_text = (
        "Package: foo\nVersion: 1.0\nFilename: pool/main/f/foo/foo_1.0_amd64.deb\n\n"
        "Package: bar\nFilename: pool/main/b/bar/bar_2.0_amd64.deb\n\n"
        "Package: baz\nVersion: 3.0\nFilename: dists/trixie/pool/stable/amd64/baz_3.0_amd64.deb\n"
    )
    compressed = lzma.compress(packages_text.encode())
    assert parse_packages_index_entries(compressed, "main/binary-amd64/Packages.xz") == [
        PackagesIndexEntry(package_name="foo", version="1.0", filename="pool/main/f/foo/foo_1.0_amd64.deb"),
        PackagesIndexEntry(
            package_name="baz", version="3.0", filename="dists/trixie/pool/stable/amd64/baz_3.0_amd64.deb"
        ),
    ]


def test_select_newest_entry_uses_dpkg_version_ordering() -> None:
    entries = [
        PackagesIndexEntry(package_name="foo", version="1.10", filename="a"),
        PackagesIndexEntry(package_name="foo", version="1.9", filename="b"),
        PackagesIndexEntry(package_name="foo", version="1.10~rc1", filename="c"),
    ]
    assert select_newest_entry(entries).filename == "a"


def test_filter_index_entries_for_components_keeps_only_configured_components() -> None:
    entries = [
        ReleaseFileEntry(path="stable/binary-amd64/Packages.gz", sha256="a", size=1),
        ReleaseFileEntry(path="nightly/binary-amd64/Packages.gz", sha256="b", size=1),
    ]
    assert [e.path for e in filter_index_entries_for_components(entries, ("stable",))] == [
        "stable/binary-amd64/Packages.gz"
    ]


def test_package_file_object_key_shares_pool_files_and_freezes_dists_files_per_cut() -> None:
    assert package_file_object_key(TIMESTAMP, "debian", "pool/main/f/foo/foo_1.0_amd64.deb") == (
        "pool/debian/pool/main/f/foo/foo_1.0_amd64.deb"
    )
    assert package_file_object_key(TIMESTAMP, "docker", "dists/trixie/pool/stable/amd64/x.deb") == (
        f"snap/{TIMESTAMP}/docker/dists/trixie/pool/stable/amd64/x.deb"
    )
    with pytest.raises(AptMirrorUnsafePathError):
        package_file_object_key(TIMESTAMP, "debian", "etc/passwd")


def test_artifact_object_key_and_url_validate_their_segments() -> None:
    assert artifact_object_key("gvisor", "20260601", "x86_64/runsc") == "artifacts/gvisor/20260601/x86_64/runsc"
    assert artifact_url("https://apt.example.com/", "uv", "0.11.7", "uv.tar.gz") == (
        "https://apt.example.com/artifacts/uv/0.11.7/uv.tar.gz"
    )
    for name, version, subpath in (("../x", "1", "f"), ("gvisor", "1 2", "f"), ("gvisor", "1", "../f")):
        with pytest.raises(AptMirrorUnsafePathError):
            artifact_object_key(name, version, subpath)


def test_snapshot_archive_url_renders_the_served_archive_root_and_validates_its_segments() -> None:
    assert snapshot_archive_url("https://apt.example.com/", TIMESTAMP, "docker") == (
        f"https://apt.example.com/snap/{TIMESTAMP}/docker"
    )
    with pytest.raises(AptMirrorInvalidTimestampError):
        snapshot_archive_url("https://apt.example.com", "2026-07-25", "docker")
    with pytest.raises(AptMirrorUnsafePathError):
        snapshot_archive_url("https://apt.example.com", TIMESTAMP, "../evil")


def test_validate_safe_subpath_rejects_traversal_and_absolute_paths() -> None:
    for bad_path in ("../etc/passwd", "a/../../b", "/etc/passwd", "a//b", "a/./b", "", "a\\b"):
        with pytest.raises(AptMirrorUnsafePathError):
            validate_safe_subpath(bad_path)
    assert validate_safe_subpath("main/f/foo/foo_1.0_amd64.deb") == "main/f/foo/foo_1.0_amd64.deb"


def test_validate_snapshot_timestamp_enforces_format() -> None:
    assert validate_snapshot_timestamp(TIMESTAMP) == TIMESTAMP
    for bad_timestamp in ("20260725", "latest", "20260725T000000", "2026-07-25T00:00:00Z"):
        with pytest.raises(AptMirrorInvalidTimestampError):
            validate_snapshot_timestamp(bad_timestamp)
