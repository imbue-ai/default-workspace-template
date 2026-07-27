"""Shared test helpers for wiring canned Debian archives into the mapping fetcher."""

import hashlib
import lzma
from collections.abc import Mapping

from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher

# Canned Packages index bodies shared by the CLI and service tests.
FOO_PACKAGES_TEXT = "Package: foo\nFilename: pool/main/f/foo/foo_1.0_amd64.deb\n"
FOO_AND_BAR_PACKAGES_TEXT = f"{FOO_PACKAGES_TEXT}\nPackage: bar\nFilename: pool/main/b/bar/bar_2.0_amd64.deb\n"


def compress_packages_index(packages_text: str) -> bytes:
    """The Packages.xz bytes for a Packages index body."""
    return lzma.compress(packages_text.encode())


def wire_canned_suite(
    fetcher: MappingUpstreamFetcher,
    snapshot_archive_base: str,
    suite: str,
    packages_xz_by_arch: Mapping[str, bytes],
) -> None:
    """Serve one suite's entry points and per-arch main Packages.xz indexes at a snapshot base URL.

    The Release/InRelease SHA256 section lists exactly the given Packages.xz
    files, so cut can freeze them and verify their checksums.
    """
    sha_lines = "".join(
        f" {hashlib.sha256(data).hexdigest()} {len(data)} main/binary-{arch}/Packages.xz\n"
        for arch, data in packages_xz_by_arch.items()
    )
    release_text = f"Suite: {suite}\nSHA256:\n{sha_lines}"
    dists = f"{snapshot_archive_base}/dists/{suite}"
    fetcher.responses_by_url[f"{dists}/InRelease"] = release_text.encode()
    fetcher.responses_by_url[f"{dists}/Release"] = release_text.encode()
    fetcher.responses_by_url[f"{dists}/Release.gpg"] = b"sig"
    for arch, data in packages_xz_by_arch.items():
        fetcher.responses_by_url[f"{dists}/main/binary-{arch}/Packages.xz"] = data
