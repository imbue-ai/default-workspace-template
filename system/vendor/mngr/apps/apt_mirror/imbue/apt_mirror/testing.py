"""Shared test helpers for wiring canned apt archives into the mapping fetcher."""

import hashlib
import lzma
from collections.abc import Mapping

from imbue.apt_mirror.data_types import ArchiveSource
from imbue.apt_mirror.data_types import DEFAULT_SNAPSHOT_BASE
from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher

# Canned Packages index bodies shared by the CLI and service tests.
FOO_PACKAGES_TEXT = "Package: foo\nVersion: 1.0\nFilename: pool/main/f/foo/foo_1.0_amd64.deb\n"
FOO_AND_BAR_PACKAGES_TEXT = (
    f"{FOO_PACKAGES_TEXT}\nPackage: bar\nVersion: 2.0\nFilename: pool/main/b/bar/bar_2.0_amd64.deb\n"
)

# A single-suite, main-only Debian archive frozen from snapshot.debian.org.
TEST_DEBIAN_ARCHIVE = ArchiveSource(
    name="debian",
    live_base="https://deb.debian.org/debian",
    snapshot_base=DEFAULT_SNAPSHOT_BASE,
    suites=("trixie",),
    components=("main",),
)


def compress_packages_index(packages_text: str) -> bytes:
    """The Packages.xz bytes for a Packages index body."""
    return lzma.compress(packages_text.encode())


def wire_canned_suite(
    fetcher: MappingUpstreamFetcher,
    archive_base: str,
    suite: str,
    packages_xz_by_arch: Mapping[str, bytes],
    component: str = "main",
) -> None:
    """Serve one suite's entry points and per-arch Packages.xz indexes at an archive root URL.

    The Release/InRelease SHA256 section lists exactly the given Packages.xz
    files, so cut can freeze them and verify their checksums.
    """
    sha_lines = "".join(
        f" {hashlib.sha256(data).hexdigest()} {len(data)} {component}/binary-{arch}/Packages.xz\n"
        for arch, data in packages_xz_by_arch.items()
    )
    release_text = f"Suite: {suite}\nSHA256:\n{sha_lines}"
    dists = f"{archive_base}/dists/{suite}"
    fetcher.responses_by_url[f"{dists}/InRelease"] = release_text.encode()
    fetcher.responses_by_url[f"{dists}/Release"] = release_text.encode()
    fetcher.responses_by_url[f"{dists}/Release.gpg"] = b"sig"
    for arch, data in packages_xz_by_arch.items():
        fetcher.responses_by_url[f"{dists}/{component}/binary-{arch}/Packages.xz"] = data
