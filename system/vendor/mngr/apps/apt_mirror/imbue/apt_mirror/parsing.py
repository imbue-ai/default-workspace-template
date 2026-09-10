"""Pure helpers for Debian archive metadata: Release/Packages parsing, path validation, R2 keys."""

import gzip
import lzma
import posixpath
import re
from collections.abc import Sequence
from typing import Final

from debian.debian_support import Version

from imbue.apt_mirror.data_types import ARTIFACTS_PREFIX
from imbue.apt_mirror.data_types import PackagesIndexEntry
from imbue.apt_mirror.data_types import ReleaseFileEntry
from imbue.apt_mirror.errors import AptMirrorInvalidTimestampError
from imbue.apt_mirror.errors import AptMirrorUnsafePathError
from imbue.imbue_common.pure import pure

_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(r"^\d{8}T\d{6}Z$")
_ARCHIVE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]*$")
# Artifact names and versions are single path segments of the characters that
# appear in release names (``gvisor``, ``20260601``, ``0.11.7``, ``v1.2.1``).
_ARTIFACT_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")

# Index files under dists/ that are never frozen: source packages, installer
# images, and pdiff histories (apt falls back to the full index when pdiffs
# are absent, so omitting them is safe and keeps cuts small).
_EXCLUDED_INDEX_SEGMENTS: Final[tuple[str, ...]] = ("/source/", "/debian-installer/", "/installer-", ".diff/")

# The two trees a Packages Filename may point into: Debian's shared top-level
# pool, or (Docker's layout) a pool nested under the suite's dists directory.
_POOL_FILENAME_PREFIX: Final[str] = "pool/"
_DISTS_FILENAME_PREFIX: Final[str] = "dists/"

# The Packages index spellings tried in order when resolving names: Debian
# publishes all three, Docker only the plain and gzip forms.
PACKAGES_INDEX_NAMES: Final[tuple[str, ...]] = ("Packages.xz", "Packages.gz", "Packages")


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
def validate_package_filename(filename: str) -> str:
    """Reject a Packages Filename that is unsafe or points outside the two known package trees.

    Index-declared paths become bucket write keys, so a traversal-shaped or
    unexpectedly-rooted Filename must raise rather than be stored.
    """
    validate_safe_subpath(filename)
    if not filename.startswith((_POOL_FILENAME_PREFIX, _DISTS_FILENAME_PREFIX)):
        raise AptMirrorUnsafePathError(filename)
    return filename


@pure
def validate_artifact_segment(segment: str) -> str:
    if not _ARTIFACT_SEGMENT_RE.match(segment):
        raise AptMirrorUnsafePathError(segment)
    return segment


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
def filter_index_entries_for_components(
    entries: list[ReleaseFileEntry],
    components: Sequence[str],
) -> list[ReleaseFileEntry]:
    """Keep the index files under the given components (a Release path's first segment names its component)."""
    return [entry for entry in entries if entry.path.split("/", 1)[0] in components]


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
def parse_packages_index_entries(packages_data: bytes, packages_path: str) -> list[PackagesIndexEntry]:
    """Extract the (name, version, Filename) of every stanza in a (compressed) Packages index.

    Stanzas missing any of the three fields are skipped: they describe nothing
    the mirror can fetch.
    """
    text = decompress_packages_index(packages_data, packages_path)
    entries: list[PackagesIndexEntry] = []
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if line.startswith(("Package: ", "Version: ", "Filename: ")):
                key, value = line.split(": ", 1)
                fields[key] = value.strip()
        if {"Package", "Version", "Filename"} <= fields.keys():
            entries.append(
                PackagesIndexEntry(
                    package_name=fields["Package"], version=fields["Version"], filename=fields["Filename"]
                )
            )
    return entries


@pure
def select_newest_entry(entries: Sequence[PackagesIndexEntry]) -> PackagesIndexEntry:
    """The entry with the highest Debian version (dpkg ordering, epochs and tildes included)."""
    return max(entries, key=lambda entry: Version(entry.version))


@pure
def dists_object_key(timestamp: str, archive: str, subpath: str) -> str:
    return f"snap/{timestamp}/{archive}/dists/{subpath}"


@pure
def pool_cache_key(archive: str, pool_subpath: str) -> str:
    return f"pool/{archive}/pool/{pool_subpath}"


@pure
def package_file_object_key(timestamp: str, archive: str, filename: str) -> str:
    """The bucket key a package file is stored under, given its Packages Filename.

    Top-level ``pool/`` files are version-unique and immutable, so they share
    one cache across every cut (the Worker's pool route). Files under
    ``dists/`` belong to the frozen index set and are stored per cut, where
    the Worker's dists route serves them.
    """
    validate_package_filename(filename)
    if filename.startswith(_POOL_FILENAME_PREFIX):
        return pool_cache_key(archive, filename[len(_POOL_FILENAME_PREFIX) :])
    return f"snap/{timestamp}/{archive}/{filename}"


@pure
def artifact_object_key(name: str, version: str, subpath: str) -> str:
    """The bucket key (and URL path) of a pinned non-apt artifact."""
    validate_artifact_segment(name)
    validate_artifact_segment(version)
    validate_safe_subpath(subpath)
    return f"{ARTIFACTS_PREFIX}/{name}/{version}/{subpath}"


@pure
def artifact_url(base_url: str, name: str, version: str, subpath: str) -> str:
    return f"{base_url.rstrip('/')}/{artifact_object_key(name, version, subpath)}"


@pure
def artifact_version_url(base_url: str, name: str, version: str) -> str:
    """The URL of one artifact version's directory (for installers that append their own per-arch paths)."""
    validate_artifact_segment(name)
    validate_artifact_segment(version)
    return f"{base_url.rstrip('/')}/{ARTIFACTS_PREFIX}/{name}/{version}"


@pure
def snapshot_archive_url(base_url: str, timestamp: str, archive: str) -> str:
    """The archive root apt sources point at for one archive frozen at a cut: ``<base>/snap/<T>/<archive>``."""
    validate_snapshot_timestamp(timestamp)
    validate_archive_name(archive)
    return f"{base_url.rstrip('/')}/snap/{timestamp}/{archive}"
