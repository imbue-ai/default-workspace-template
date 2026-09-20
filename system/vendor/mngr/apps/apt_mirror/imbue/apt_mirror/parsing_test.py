import lzma

import pytest

from imbue.apt_mirror.data_types import DockerfileBaseImage
from imbue.apt_mirror.data_types import InstalledPackage
from imbue.apt_mirror.data_types import PackagesIndexEntry
from imbue.apt_mirror.data_types import ReleaseFileEntry
from imbue.apt_mirror.data_types import SnapshotPackageMismatch
from imbue.apt_mirror.errors import AptMirrorInvalidTimestampError
from imbue.apt_mirror.errors import AptMirrorTemplateBaseImageError
from imbue.apt_mirror.errors import AptMirrorUnsafePathError
from imbue.apt_mirror.parsing import artifact_object_key
from imbue.apt_mirror.parsing import artifact_url
from imbue.apt_mirror.parsing import by_hash_path_for_entry
from imbue.apt_mirror.parsing import filter_index_entries_for_architectures
from imbue.apt_mirror.parsing import filter_index_entries_for_components
from imbue.apt_mirror.parsing import find_packages_newer_than_or_absent_from_index
from imbue.apt_mirror.parsing import package_file_object_key
from imbue.apt_mirror.parsing import parse_dockerfile_base_image
from imbue.apt_mirror.parsing import parse_dockerfile_from_image
from imbue.apt_mirror.parsing import parse_dpkg_status_installed_packages
from imbue.apt_mirror.parsing import parse_image_reference
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


def test_parse_dpkg_status_keeps_only_packages_whose_version_dpkg_holds() -> None:
    status_text = (
        "Package: libc6\n"
        "Status: install ok installed\n"
        "Version: 2.41-12+deb13u3\n"
        "Architecture: amd64\n"
        "\n"
        "Package: old-config\n"
        "Status: deinstall ok config-files\n"
        "Version: 1.0\n"
        "\n"
        "Package: half-done\n"
        "Status: install ok half-installed\n"
        "Version: 2.0\n"
        "\n"
        "Package: only-unpacked\n"
        "Status: install ok unpacked\n"
        "Version: 2.5\n"
        "\n"
        "Package: perl-base\n"
        "Status: install ok installed\n"
        "Version: 5.40.1-6\n"
        "\n"
        "Package: held-lib\n"
        "Status: hold ok installed\n"
        "Version: 3.0\n"
        "\n"
        "Package: man-db\n"
        "Status: install ok triggers-pending\n"
        "Version: 2.13.0-1\n"
        "\n"
        "Package: libc-bin\n"
        "Status: install ok triggers-awaited\n"
        "Version: 2.41-12+deb13u3\n"
    )

    installed = parse_dpkg_status_installed_packages(status_text)

    assert installed == [
        InstalledPackage(name="libc6", version="2.41-12+deb13u3"),
        InstalledPackage(name="perl-base", version="5.40.1-6"),
        InstalledPackage(name="held-lib", version="3.0"),
        InstalledPackage(name="man-db", version="2.13.0-1"),
        InstalledPackage(name="libc-bin", version="2.41-12+deb13u3"),
    ]


def test_parse_dpkg_status_of_an_empty_file_is_empty() -> None:
    assert parse_dpkg_status_installed_packages("") == []


def test_find_packages_newer_than_or_absent_from_index_flags_only_newer_or_unlisted_installs() -> None:
    # libc6 is listed by both trixie and trixie-security: the newest listing wins under dpkg ordering.
    index_entries = [
        PackagesIndexEntry(package_name="libc6", version="2.41-12", filename="a"),
        PackagesIndexEntry(package_name="libc6", version="2.41-12+deb13u3", filename="b"),
        PackagesIndexEntry(package_name="perl-base", version="5.40.1-6", filename="c"),
        PackagesIndexEntry(package_name="tzdata", version="2025b-1", filename="d"),
        PackagesIndexEntry(package_name="tzdata", version="2025b-1~deb13u1", filename="e"),
    ]
    installed = [
        InstalledPackage(name="libc6", version="2.41-13"),
        InstalledPackage(name="perl-base", version="5.40.1-5"),
        InstalledPackage(name="tzdata", version="2025b-1"),
        InstalledPackage(name="removed-pkg", version="1.0"),
    ]

    mismatches = find_packages_newer_than_or_absent_from_index(installed, index_entries)

    assert mismatches == [
        SnapshotPackageMismatch(name="libc6", installed_version="2.41-13", newest_index_version="2.41-12+deb13u3"),
        SnapshotPackageMismatch(name="removed-pkg", installed_version="1.0", newest_index_version=None),
    ]
    assert [mismatch.text for mismatch in mismatches] == [
        "libc6=2.41-13 (index lists 2.41-12+deb13u3)",
        "removed-pkg=1.0 (index lists nothing)",
    ]


_PINNED_BASE_IMAGE_REF = (
    "python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)


@pytest.mark.parametrize(
    "from_line",
    [
        f"from {_PINNED_BASE_IMAGE_REF} AS base",
        f"FROM --platform=$BUILDPLATFORM {_PINNED_BASE_IMAGE_REF}",
        f"FROM\t{_PINNED_BASE_IMAGE_REF}",
    ],
)
def test_parse_dockerfile_base_image_returns_the_first_from_reference(from_line: str) -> None:
    dockerfile_text = f"# FROM in a comment is ignored\nARG X=1\n{from_line}\nFROM alpine:3.20\n"

    assert parse_dockerfile_base_image(dockerfile_text) == _PINNED_BASE_IMAGE_REF


@pytest.mark.parametrize(
    ("dockerfile_text", "message"),
    [
        ("FROM python:3.12-slim-trixie\n", "not digest-pinned"),
        ("FROM --platform=linux/amd64 python:3.12-slim-trixie\n", "not digest-pinned"),
        ("FROM python:3.12-slim-trixie@sha256:57cd7c3a\n", "not digest-pinned"),
        ("FROM python:3.12-slim-trixie@sha256:${BASE_DIGEST}\n", "not digest-pinned"),
        ("RUN true\n", "no FROM line"),
    ],
)
def test_parse_dockerfile_base_image_rejects_floating_or_missing_bases(dockerfile_text: str, message: str) -> None:
    with pytest.raises(AptMirrorTemplateBaseImageError, match=message):
        parse_dockerfile_base_image(dockerfile_text)


@pytest.mark.parametrize(
    ("dockerfile_text", "expected"),
    [
        (
            "FROM python:3.12-slim-trixie\nRUN true\n",
            DockerfileBaseImage(image_name="python:3.12-slim-trixie", digest=None),
        ),
        (
            f"FROM --platform=linux/amd64 {_PINNED_BASE_IMAGE_REF} AS base\n",
            DockerfileBaseImage(
                image_name="python:3.12-slim-trixie",
                digest="sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de",
            ),
        ),
        ("# FROM in a comment\nfrom alpine\n", DockerfileBaseImage(image_name="alpine", digest=None)),
    ],
    ids=["floating", "pinned-with-options", "lowercase-bare-name"],
)
def test_parse_dockerfile_from_image_reads_floating_and_pinned_bases(
    dockerfile_text: str, expected: DockerfileBaseImage
) -> None:
    base_image = parse_dockerfile_from_image(dockerfile_text)

    assert base_image == expected
    assert base_image.is_digest_pinned is (expected.digest is not None)


def test_parse_image_reference_round_trips_the_reference() -> None:
    assert parse_image_reference(_PINNED_BASE_IMAGE_REF).image_ref == _PINNED_BASE_IMAGE_REF
    assert parse_image_reference("python:3.12-slim-trixie").image_ref == "python:3.12-slim-trixie"


@pytest.mark.parametrize(
    "image_ref",
    [
        "",
        "python:3.12-slim-trixie@sha256:57cd7c3a",
        "python:3.12-slim-trixie@sha256:${BASE_DIGEST}",
        "@sha256:" + "0" * 64,
    ],
)
def test_parse_image_reference_rejects_malformed_references(image_ref: str) -> None:
    with pytest.raises(AptMirrorTemplateBaseImageError):
        parse_image_reference(image_ref)
