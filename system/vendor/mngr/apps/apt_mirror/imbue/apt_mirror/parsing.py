"""Pure helpers for Debian archive metadata: Release/Packages parsing, path validation, R2 keys.

Also the rootfs side of the template pin check: dpkg status and Dockerfile
FROM parsing, and the comparison of installed packages against a frozen index.
"""

import gzip
import lzma
import posixpath
import re
from collections.abc import Sequence
from typing import Final

from debian.debian_support import Version

from imbue.apt_mirror.data_types import ARTIFACTS_PREFIX
from imbue.apt_mirror.data_types import DockerfileBaseImage
from imbue.apt_mirror.data_types import InstalledPackage
from imbue.apt_mirror.data_types import PackagesIndexEntry
from imbue.apt_mirror.data_types import ReleaseFileEntry
from imbue.apt_mirror.data_types import SnapshotPackageMismatch
from imbue.apt_mirror.errors import AptMirrorInvalidTimestampError
from imbue.apt_mirror.errors import AptMirrorTemplateBaseImageError
from imbue.apt_mirror.errors import AptMirrorUnsafePathError
from imbue.imbue_common.pure import pure

_TIMESTAMP_RE: Final[re.Pattern[str]] = re.compile(r"^\d{8}T\d{6}Z$")
_ARCHIVE_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]*$")
# Artifact names and versions are single path segments of the characters that
# appear in release names (``gvisor``, ``20260601``, ``0.11.7``, ``v1.2.1``).
_ARTIFACT_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# The content digest an image reference pins: the part after ``@`` in ``<name>[:tag]@sha256:<64 hex digits>``.
_IMAGE_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")

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

# The state words of a dpkg ``Status`` (``<want> <flag> <state>``) under which
# the package is unpacked and configured, so apt holds its version and will
# not downgrade it, whatever the selection (install or hold). The trigger
# states only defer post-configuration trigger processing.
_DPKG_STATES_HOLDING_A_VERSION: Final[tuple[str, ...]] = ("installed", "triggers-awaited", "triggers-pending")


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
def _parse_stanza_fields(control_text: str, field_names: tuple[str, ...]) -> list[dict[str, str]]:
    """The named single-line fields present in each blank-line-separated stanza of a Debian control-style file."""
    field_prefixes = tuple(f"{name}: " for name in field_names)
    fields_by_stanza: list[dict[str, str]] = []
    for stanza in control_text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if line.startswith(field_prefixes):
                key, value = line.split(": ", 1)
                fields[key] = value.strip()
        fields_by_stanza.append(fields)
    return fields_by_stanza


@pure
def parse_packages_index_entries(packages_data: bytes, packages_path: str) -> list[PackagesIndexEntry]:
    """Extract the (name, version, Filename) of every stanza in a (compressed) Packages index.

    Stanzas missing any of the three fields are skipped: they describe nothing
    the mirror can fetch.
    """
    text = decompress_packages_index(packages_data, packages_path)
    entries: list[PackagesIndexEntry] = []
    for fields in _parse_stanza_fields(text, ("Package", "Version", "Filename")):
        if {"Package", "Version", "Filename"} <= fields.keys():
            entries.append(
                PackagesIndexEntry(
                    package_name=fields["Package"], version=fields["Version"], filename=fields["Filename"]
                )
            )
    return entries


@pure
def parse_dpkg_status_installed_packages(status_text: str) -> list[InstalledPackage]:
    """The (name, version) of every package a ``/var/lib/dpkg/status`` file reports as installed and configured.

    Held packages count (``hold ok installed``), as do packages only awaiting
    trigger processing. Stanzas in any other state (not-installed,
    config-files, half-installed, unpacked, half-configured) are skipped: apt
    does not hold their version, so they cannot conflict with a frozen index.
    """
    installed: list[InstalledPackage] = []
    for fields in _parse_stanza_fields(status_text, ("Package", "Version", "Status")):
        status_words = fields.get("Status", "").split()
        if not status_words or status_words[-1] not in _DPKG_STATES_HOLDING_A_VERSION:
            continue
        if {"Package", "Version"} <= fields.keys():
            installed.append(InstalledPackage(name=fields["Package"], version=fields["Version"]))
    return installed


@pure
def find_packages_newer_than_or_absent_from_index(
    installed: Sequence[InstalledPackage], index_entries: Sequence[PackagesIndexEntry]
) -> list[SnapshotPackageMismatch]:
    """The installed packages a frozen index cannot serve: newer than every version it lists, or not listed at all.

    apt never downgrades, so a rootfs shipping such a package makes any install
    that depends on the index's version of it unsatisfiable. Older installed
    versions are fine: apt upgrades them to what the index lists.
    """
    newest_by_name: dict[str, str] = {}
    for entry in index_entries:
        listed = newest_by_name.get(entry.package_name)
        if listed is None or Version(entry.version) > Version(listed):
            newest_by_name[entry.package_name] = entry.version
    mismatches: list[SnapshotPackageMismatch] = []
    for package in installed:
        newest = newest_by_name.get(package.name)
        if newest is None or Version(package.version) > Version(newest):
            mismatches.append(
                SnapshotPackageMismatch(
                    name=package.name, installed_version=package.version, newest_index_version=newest
                )
            )
    return mismatches


@pure
def parse_image_reference(image_ref: str) -> DockerfileBaseImage:
    """Split ``<name>[:tag][@sha256:<digest>]`` into its name and optional digest.

    Raises AptMirrorTemplateBaseImageError when the reference is empty or
    carries an ``@`` that is not followed by a full sha256 digest (a truncated
    digest, or an unexpanded ``${VAR}``): such a reference is not pinned, and
    reading it as a floating one would hide the typo.
    """
    image_name, separator, digest = image_ref.partition("@")
    if not image_name:
        raise AptMirrorTemplateBaseImageError(f"image reference {image_ref!r} names no image")
    if not separator:
        return DockerfileBaseImage(image_name=image_name, digest=None)
    if not _IMAGE_DIGEST_RE.match(digest):
        raise AptMirrorTemplateBaseImageError(
            f"image reference {image_ref!r} is not digest-pinned (expected image@sha256:<64 hex digits>)"
        )
    return DockerfileBaseImage(image_name=image_name, digest=digest)


@pure
def parse_dockerfile_from_image(dockerfile_text: str) -> DockerfileBaseImage:
    """The image reference of a Dockerfile's first ``FROM`` line, whether it pins a digest or floats on its tag.

    Raises AptMirrorTemplateBaseImageError when there is no ``FROM`` line or
    its reference is malformed (see :func:`parse_image_reference`).
    """
    for line in dockerfile_text.splitlines():
        tokens = line.split()
        if not tokens or tokens[0].upper() != "FROM":
            continue
        # FROM may carry options (``--platform=...``) ahead of the reference.
        operands = [token for token in tokens[1:] if not token.startswith("--")]
        return parse_image_reference(operands[0] if operands else "")
    raise AptMirrorTemplateBaseImageError("Dockerfile has no FROM line")


@pure
def parse_dockerfile_base_image(dockerfile_text: str) -> str:
    """The digest-pinned image reference of a Dockerfile's first ``FROM`` line.

    Raises AptMirrorTemplateBaseImageError when there is no ``FROM`` or the
    reference floats (no ``@sha256:`` digest): a tag can move to a newer
    Debian point release under a frozen apt snapshot, which is exactly the
    breakage the pin exists to prevent.
    """
    base_image = parse_dockerfile_from_image(dockerfile_text)
    if not base_image.is_digest_pinned:
        raise AptMirrorTemplateBaseImageError(
            f"Dockerfile base image {base_image.image_ref!r} is not digest-pinned "
            "(expected image@sha256:<64 hex digits>)"
        )
    return base_image.image_ref


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
