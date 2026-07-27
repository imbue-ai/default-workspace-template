"""Pure helpers for Debian archive metadata: Release/Packages parsing, path validation, R2 keys."""

import gzip
import lzma
import posixpath
import re
from typing import Final

from imbue.apt_mirror.data_types import ReleaseFileEntry
from imbue.apt_mirror.errors import AptMirrorInvalidTimestampError
from imbue.apt_mirror.errors import AptMirrorUnsafePathError
from imbue.imbue_common.pure import pure

_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(r"^\d{8}T\d{6}Z$")
_ARCHIVE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]*$")

# Index files under dists/ that are never frozen: source packages, installer
# images, and pdiff histories (apt falls back to the full index when pdiffs
# are absent, so omitting them is safe and keeps cuts small).
_EXCLUDED_INDEX_SEGMENTS: Final[tuple[str, ...]] = ("/source/", "/debian-installer/", "/installer-", ".diff/")


@pure
def validate_snapshot_timestamp(timestamp: str) -> str:
    if not _TIMESTAMP_RE.match(timestamp):
        raise AptMirrorInvalidTimestampError(timestamp)
    return timestamp


@pure
def validate_archive_name(archive: str) -> str:
    if not _ARCHIVE_RE.match(archive):
        raise AptMirrorUnsafePathError(archive)
    return archive


@pure
def validate_safe_subpath(subpath: str) -> str:
    """Reject paths that could escape the archive tree or alias other keys."""
    if not subpath or subpath.startswith("/") or "\\" in subpath:
        raise AptMirrorUnsafePathError(subpath)
    normalized = posixpath.normpath(subpath)
    if normalized != subpath or any(segment in ("..", ".", "") for segment in subpath.split("/")):
        raise AptMirrorUnsafePathError(subpath)
    return subpath


@pure
def parse_release_sha256_entries(release_text: str) -> list[ReleaseFileEntry]:
    """Parse the SHA256 section of a Release file into file entries."""
    entries: list[ReleaseFileEntry] = []
    is_in_sha256_section = False
    for line in release_text.splitlines():
        if not line.startswith(" "):
            is_in_sha256_section = line.strip() == "SHA256:"
            continue
        if not is_in_sha256_section:
            continue
        parts = line.split()
        if len(parts) != 3:
            continue
        sha256, size_str, path = parts
        if not size_str.isdigit():
            continue
        entries.append(ReleaseFileEntry(path=path, sha256=sha256, size=int(size_str)))
    return entries


@pure
def filter_index_entries_for_architectures(
    entries: list[ReleaseFileEntry],
    architectures: tuple[str, ...],
) -> list[ReleaseFileEntry]:
    """Keep the index files apt can request for the given binary architectures.

    Includes per-arch package indexes (binary-<arch> plus binary-all), Contents
    files, translations, and command-not-found indexes; excludes source
    indexes, installer images, and pdiff histories.
    """
    wanted_arches = tuple(architectures) + ("all",)
    filtered: list[ReleaseFileEntry] = []
    for entry in entries:
        if any(segment in f"/{entry.path}" for segment in _EXCLUDED_INDEX_SEGMENTS):
            continue
        is_arch_specific = "binary-" in entry.path or "Contents-" in entry.path or "Commands-" in entry.path
        if not is_arch_specific:
            filtered.append(entry)
            continue
        if any(
            f"binary-{arch}/" in entry.path or f"Contents-{arch}" in entry.path or f"Commands-{arch}" in entry.path
            for arch in wanted_arches
        ):
            filtered.append(entry)
    return filtered


@pure
def by_hash_path_for_entry(entry: ReleaseFileEntry) -> str:
    """The by-hash alias apt requests for an index when Acquire-By-Hash is on."""
    directory = posixpath.dirname(entry.path)
    prefix = f"{directory}/" if directory else ""
    return f"{prefix}by-hash/SHA256/{entry.sha256}"


@pure
def decompress_packages_index(packages_data: bytes, packages_path: str) -> str:
    if packages_path.endswith(".xz"):
        return lzma.decompress(packages_data).decode("utf-8", errors="replace")
    elif packages_path.endswith(".gz"):
        return gzip.decompress(packages_data).decode("utf-8", errors="replace")
    else:
        return packages_data.decode("utf-8", errors="replace")


@pure
def parse_packages_name_and_pool_path_pairs(packages_data: bytes, packages_path: str) -> list[tuple[str, str]]:
    """Extract (package name, Filename) pairs from a (compressed) Packages index."""
    text = decompress_packages_index(packages_data, packages_path)
    pairs: list[tuple[str, str]] = []
    current_package = ""
    for line in text.splitlines():
        if line.startswith("Package: "):
            current_package = line[len("Package: ") :].strip()
        elif line.startswith("Filename: ") and current_package:
            pairs.append((current_package, line[len("Filename: ") :].strip()))
        else:
            pass
    return pairs


@pure
def dists_object_key(timestamp: str, archive: str, subpath: str) -> str:
    return f"snap/{timestamp}/{archive}/dists/{subpath}"


@pure
def pool_cache_key(archive: str, pool_subpath: str) -> str:
    return f"pool/{archive}/pool/{pool_subpath}"
