from functools import cached_property
from typing import Final

from pydantic import Field
from pydantic import computed_field

from imbue.imbue_common.frozen_model import FrozenModel

# The public base URL the mirror is served at (the Worker's custom domain).
APT_MIRROR_PUBLIC_BASE_URL: Final[str] = "https://apt.imbuepackages.com"

# The bucket prefix (and URL path segment) under which pinned non-apt
# artifacts live: ``artifacts/<name>/<version>/<subpath>``. Immutable and
# explicit -- a missing object is a 404, never an upstream fetch.
ARTIFACTS_PREFIX: Final[str] = "artifacts"

# snapshot.debian.org base; ``<base>/<archive>/<T>/`` is a full archive root
# frozen at ``T``. Used for cuts (authoritative index set at ``T``) and as the
# pool fallback for files already superseded on the live archive.
DEFAULT_SNAPSHOT_BASE: Final[str] = "https://snapshot.debian.org/archive"

DEFAULT_ARCHITECTURES: Final[tuple[str, ...]] = ("amd64", "arm64")


class ArchiveSource(FrozenModel):
    """One upstream apt archive the mirror freezes: where its indexes and packages come from.

    An archive with a ``snapshot_base`` (the Debian archives) is cut from
    snapshot.debian.org at the timestamp and its pool files are shared across
    cuts. An archive without one (Docker's repo) has no history service, so a
    cut freezes its live indexes as of the moment it runs, and every listed
    package file is frozen alongside them under the same cut prefix.
    """

    name: str = Field(description="Archive name, the ``<archive>`` segment of the served URL")
    live_base: str = Field(description="The live upstream archive root (holds dists/ and the package files)")
    snapshot_base: str | None = Field(
        description="snapshot.debian.org-style base whose ``<archive>/<T>/`` roots are frozen archives, or None"
    )
    suites: tuple[str, ...] = Field(description="Suites (dists/<suite>) frozen by a cut")
    components: tuple[str, ...] = Field(
        min_length=1,
        description="Components whose indexes are frozen and searched; the first is required to be present",
    )

    @property
    def required_component(self) -> str:
        return self.components[0]

    def snapshot_root(self, timestamp: str) -> str | None:
        """The frozen archive root at ``timestamp`` (``<snapshot_base>/<name>/<T>``), or None without a snapshot service."""
        if self.snapshot_base is None:
            return None
        return f"{self.snapshot_base}/{self.name}/{timestamp}"


DEBIAN_ARCHIVE: Final[ArchiveSource] = ArchiveSource(
    name="debian",
    live_base="https://deb.debian.org/debian",
    snapshot_base=DEFAULT_SNAPSHOT_BASE,
    suites=("trixie", "trixie-updates"),
    components=("main", "contrib", "non-free", "non-free-firmware"),
)
DEBIAN_SECURITY_ARCHIVE: Final[ArchiveSource] = ArchiveSource(
    name="debian-security",
    live_base="https://deb.debian.org/debian-security",
    snapshot_base=DEFAULT_SNAPSHOT_BASE,
    suites=("trixie-security",),
    components=("main", "contrib", "non-free", "non-free-firmware"),
)
# Docker's apt repo: the pinned engine the slice guest images install. Its
# package files sit under dists/<suite>/pool/ (not a top-level pool/), so
# they are frozen per cut with the indexes.
DOCKER_ARCHIVE: Final[ArchiveSource] = ArchiveSource(
    name="docker",
    live_base="https://download.docker.com/linux/debian",
    snapshot_base=None,
    suites=("trixie",),
    components=("stable",),
)
DEFAULT_ARCHIVES: Final[tuple[ArchiveSource, ...]] = (DEBIAN_ARCHIVE, DEBIAN_SECURITY_ARCHIVE, DOCKER_ARCHIVE)


class ReleaseFileEntry(FrozenModel):
    """One file row from a Release file's SHA256 section."""

    path: str = Field(description="Path relative to the dists/<suite>/ directory")
    sha256: str = Field(description="Hex sha256 the Release file declares for this file")
    size: int = Field(description="Size in bytes the Release file declares")


class PackagesIndexEntry(FrozenModel):
    """One binary package stanza from a Packages index."""

    package_name: str = Field(description="The Package field")
    version: str = Field(description="The Version field (Debian version string)")
    filename: str = Field(description="The Filename field: the package file path relative to the archive root")


class PackageSpec(FrozenModel):
    """One package-list entry: a package name, optionally pinned to an exact version (``name=version``)."""

    name: str = Field(description="The package name")
    version: str | None = Field(description="An exact Debian version to freeze, or None for the newest in the index")

    @property
    def text(self) -> str:
        return self.name if self.version is None else f"{self.name}={self.version}"


class AptMirrorCutRequest(FrozenModel):
    """Request to freeze the index set for a new snapshot timestamp."""

    timestamp: str = Field(description="snapshot.debian.org timestamp, e.g. 20260725T000000Z")
    architectures: tuple[str, ...] = Field(
        default=DEFAULT_ARCHITECTURES,
        description="Binary architectures whose indexes are frozen (plus arch-independent files)",
    )
    archives: tuple[ArchiveSource, ...] = Field(default=DEFAULT_ARCHIVES, description="Archives to freeze")


class AptMirrorCutResult(FrozenModel):
    """Outcome of a cut: how many index objects were stored or already present."""

    timestamp: str = Field(description="The cut snapshot timestamp")
    stored_index_count: int = Field(description="Index objects newly stored in the bucket")
    already_present_count: int = Field(description="Index objects that were already stored (idempotent re-cut)")
    missing_upstream_count: int = Field(
        description="Files the index source did not serve (the optional detached Release pair or Release-listed indexes)"
    )


class ResolvedPackageFile(FrozenModel):
    """One package file a listed package resolves to in a cut timestamp's indexes."""

    archive: str = Field(description="Archive name, e.g. debian")
    filename: str = Field(description="The package file path relative to the archive root (the Packages Filename)")
    package_name: str = Field(description="The listed package name this file belongs to")

    @property
    def qualified_path(self) -> str:
        """The archive-qualified package file path, as reported for cache gaps."""
        return f"{self.archive}/{self.filename}"


class PackageListResolution(FrozenModel):
    """Outcome of resolving package specs against a cut timestamp's Packages indexes."""

    resolved_files: tuple[ResolvedPackageFile, ...] = Field(
        description="Deduplicated package files, in package-list order within each index"
    )
    unresolved_specs: tuple[str, ...] = Field(
        description="Listed specs (``name`` or ``name=version``) found in no index"
    )


class AptMirrorCompletenessResult(FrozenModel):
    """Base for results that report package-file gaps against the package lists."""

    missing_paths: tuple[str, ...] = Field(description="Listed package files absent from the bucket")
    unresolved_specs: tuple[str, ...] = Field(description="Listed specs found in no index")

    @computed_field
    @cached_property
    def is_complete(self) -> bool:
        return not self.missing_paths and not self.unresolved_specs


class AptMirrorWarmResult(AptMirrorCompletenessResult):
    """Outcome of warming a cut timestamp's listed packages into the bucket."""

    timestamp: str = Field(description="The warmed snapshot timestamp")
    examined_count: int = Field(description="Package files examined")
    fetched_count: int = Field(description="Package files newly fetched into the bucket")
    already_cached_count: int = Field(description="Package files already in the bucket")


class AptMirrorVerifyResult(AptMirrorCompletenessResult):
    """Outcome of a read-only check of a cut timestamp against the package lists."""

    timestamp: str = Field(description="The verified snapshot timestamp")
    cached_count: int = Field(description="Listed package files present in the bucket")
