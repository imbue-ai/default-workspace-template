from functools import cached_property
from typing import Final

from pydantic import Field
from pydantic import computed_field

from imbue.imbue_common.frozen_model import FrozenModel

# Live archive bases read through on pool misses, keyed by archive name.
DEFAULT_UPSTREAM_BY_ARCHIVE: Final[dict[str, str]] = {
    "debian": "https://deb.debian.org/debian",
    "debian-security": "https://deb.debian.org/debian-security",
}

# snapshot.debian.org base; ``<base>/<archive>/<T>/`` is a full archive root
# frozen at ``T``. Used for cuts (authoritative index set at ``T``) and as the
# pool fallback for files already superseded on the live archive.
DEFAULT_SNAPSHOT_BASE: Final[str] = "https://snapshot.debian.org/archive"

DEFAULT_SUITES_BY_ARCHIVE: Final[dict[str, tuple[str, ...]]] = {
    "debian": ("trixie", "trixie-updates"),
    "debian-security": ("trixie-security",),
}
DEFAULT_ARCHITECTURES: Final[tuple[str, ...]] = ("amd64", "arm64")


class ReleaseFileEntry(FrozenModel):
    """One file row from a Release file's SHA256 section."""

    path: str = Field(description="Path relative to the dists/<suite>/ directory")
    sha256: str = Field(description="Hex sha256 the Release file declares for this file")
    size: int = Field(description="Size in bytes the Release file declares")


class AptMirrorCutRequest(FrozenModel):
    """Request to freeze the index set for a new snapshot timestamp."""

    timestamp: str = Field(description="snapshot.debian.org timestamp, e.g. 20260725T000000Z")
    architectures: tuple[str, ...] = Field(
        default=DEFAULT_ARCHITECTURES,
        description="Binary architectures whose indexes are frozen (plus arch-independent files)",
    )
    suites_by_archive: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: dict(DEFAULT_SUITES_BY_ARCHIVE),
        description="Suites to freeze, keyed by archive name",
    )


class AptMirrorCutResult(FrozenModel):
    """Outcome of a cut: how many index objects were stored or already present."""

    timestamp: str = Field(description="The cut snapshot timestamp")
    stored_index_count: int = Field(description="Index objects newly stored in the bucket")
    already_present_count: int = Field(description="Index objects that were already stored (idempotent re-cut)")
    missing_upstream_count: int = Field(
        description="Files snapshot.debian.org did not serve (the optional detached Release pair or Release-listed indexes)"
    )


class ResolvedPoolFile(FrozenModel):
    """One pool file a listed package resolves to in a cut timestamp's indexes."""

    archive: str = Field(description="Archive name, e.g. debian")
    pool_subpath: str = Field(description="Pool path relative to pool/, as the serve route receives it")
    package_name: str = Field(description="The listed package name this file belongs to")

    @property
    def qualified_pool_path(self) -> str:
        """The archive-qualified pool path, as reported for cache gaps."""
        return f"{self.archive}/pool/{self.pool_subpath}"


class PackageListResolution(FrozenModel):
    """Outcome of resolving package names against a cut timestamp's Packages indexes."""

    resolved_files: tuple[ResolvedPoolFile, ...] = Field(description="Deduplicated pool files, in index order")
    unknown_package_names: tuple[str, ...] = Field(description="Listed names found in no index")


class AptMirrorCompletenessResult(FrozenModel):
    """Base for results that report pool-cache gaps against the package lists."""

    missing_pool_paths: tuple[str, ...] = Field(description="Listed pool files absent from the cache")
    unknown_package_names: tuple[str, ...] = Field(description="Listed names found in no index")

    @computed_field
    @cached_property
    def is_complete(self) -> bool:
        return not self.missing_pool_paths and not self.unknown_package_names


class AptMirrorWarmResult(AptMirrorCompletenessResult):
    """Outcome of warming a cut timestamp's listed packages into the pool cache."""

    timestamp: str = Field(description="The warmed snapshot timestamp")
    examined_count: int = Field(description="Pool files examined")
    fetched_count: int = Field(description="Pool files newly fetched into the cache")
    already_cached_count: int = Field(description="Pool files already in the cache")


class AptMirrorVerifyResult(AptMirrorCompletenessResult):
    """Outcome of a read-only check of a cut timestamp against the package lists."""

    timestamp: str = Field(description="The verified snapshot timestamp")
    cached_count: int = Field(description="Listed pool files present in the cache")
