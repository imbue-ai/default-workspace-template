"""Cut/warm/verify operations for the snapshot-pinned apt mirror.

The mirror's serve path is the Cloudflare Worker in ``worker/`` reading the
same R2 bucket; this service only writes to the bucket (freezing index sets
and pre-fetching pool files), so it runs wherever the operator runs the CLI.
"""

import concurrent.futures
import hashlib
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.apt_mirror.data_types import AptMirrorCutRequest
from imbue.apt_mirror.data_types import AptMirrorCutResult
from imbue.apt_mirror.data_types import AptMirrorVerifyResult
from imbue.apt_mirror.data_types import AptMirrorWarmResult
from imbue.apt_mirror.data_types import DEFAULT_SNAPSHOT_BASE
from imbue.apt_mirror.data_types import DEFAULT_UPSTREAM_BY_ARCHIVE
from imbue.apt_mirror.data_types import PackageListResolution
from imbue.apt_mirror.data_types import ResolvedPoolFile
from imbue.apt_mirror.errors import AptMirrorChecksumMismatchError
from imbue.apt_mirror.errors import AptMirrorNotCutError
from imbue.apt_mirror.errors import AptMirrorObjectNotFoundError
from imbue.apt_mirror.interfaces import AptMirrorStorageInterface
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface
from imbue.apt_mirror.parsing import by_hash_path_for_entry
from imbue.apt_mirror.parsing import dists_object_key
from imbue.apt_mirror.parsing import filter_index_entries_for_architectures
from imbue.apt_mirror.parsing import parse_packages_name_and_pool_path_pairs
from imbue.apt_mirror.parsing import parse_release_sha256_entries
from imbue.apt_mirror.parsing import pool_cache_key
from imbue.apt_mirror.parsing import validate_archive_name
from imbue.apt_mirror.parsing import validate_safe_subpath
from imbue.apt_mirror.parsing import validate_snapshot_timestamp
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel

# Components scanned for Packages indexes when resolving package names. dwt
# sources only enable main, but the cut freezes whatever the Release lists,
# so resolution tolerates any subset beyond main being absent.
_COMPONENTS: tuple[str, ...] = ("main", "contrib", "non-free", "non-free-firmware")


class _SuiteCutCounts(FrozenModel):
    """Index-object counts from freezing one suite."""

    stored_count: int = Field(description="Index objects newly stored in the bucket")
    already_present_count: int = Field(description="Index objects that were already stored")
    missing_upstream_count: int = Field(description="Files snapshot.debian.org did not serve")


class _WarmOutcome(UpperCaseStrEnum):
    """What happened to a single pool file during a warm pass."""

    FETCHED = auto()
    ALREADY_CACHED = auto()
    MISSING = auto()


class AptMirrorService(MutableModel):
    """Writes frozen index sets and pre-fetched pool files into the mirror bucket."""

    storage: AptMirrorStorageInterface = Field(frozen=True, description="The mirror bucket")
    fetcher: UpstreamFetcherInterface = Field(frozen=True, description="HTTP fetcher for upstream archives")
    upstream_by_archive: dict[str, str] = Field(
        frozen=True,
        description="Live archive base URL per archive name",
        default_factory=lambda: dict(DEFAULT_UPSTREAM_BY_ARCHIVE),
    )
    snapshot_base: str = Field(
        frozen=True,
        default=DEFAULT_SNAPSHOT_BASE,
        description="snapshot.debian.org archive base URL",
    )

    def cut(self, request: AptMirrorCutRequest) -> AptMirrorCutResult:
        """Freeze the index set for a timestamp into the bucket. Idempotent."""
        timestamp = validate_snapshot_timestamp(request.timestamp)
        stored_count = 0
        present_count = 0
        missing_count = 0
        for archive, suites in request.suites_by_archive.items():
            validate_archive_name(archive)
            for suite in suites:
                with log_span("Freezing indexes for {}/{} at {}", archive, suite, timestamp):
                    counts = self._cut_suite(timestamp, archive, suite, tuple(request.architectures))
                stored_count += counts.stored_count
                present_count += counts.already_present_count
                missing_count += counts.missing_upstream_count
        return AptMirrorCutResult(
            timestamp=timestamp,
            stored_index_count=stored_count,
            already_present_count=present_count,
            missing_upstream_count=missing_count,
        )

    def _cut_suite(
        self,
        timestamp: str,
        archive: str,
        suite: str,
        architectures: tuple[str, ...],
    ) -> _SuiteCutCounts:
        """Freeze one suite's indexes into the bucket."""
        snapshot_dists = f"{self.snapshot_base}/{archive}/{timestamp}/dists/{suite}"
        stored_count = 0
        present_count = 0
        missing_count = 0

        # The signed entry points come first: InRelease (inline-signed) is
        # mandatory; the detached Release/Release.gpg pair is stored when
        # present so older apt configurations keep working.
        release_data: bytes | None = None
        for name in ("InRelease", "Release", "Release.gpg"):
            data = self.fetcher.fetch(f"{snapshot_dists}/{name}")
            if data is None:
                if name == "InRelease":
                    raise AptMirrorObjectNotFoundError(f"{archive}/dists/{suite}/InRelease at {timestamp}")
                missing_count += 1
                continue
            if name == "Release":
                release_data = data
            key = dists_object_key(timestamp, archive, f"{suite}/{name}")
            if self.storage.has_object(key):
                present_count += 1
            else:
                self.storage.put_object(key, data)
                stored_count += 1

        # The InRelease body doubles as the Release manifest when the detached
        # Release file is absent (its clearsigned payload carries the same
        # SHA256 section, which the parser reads regardless of signature armor).
        manifest_text = (release_data if release_data is not None else b"").decode("utf-8", errors="replace")
        if release_data is None:
            in_release = self.storage.get_object(dists_object_key(timestamp, archive, f"{suite}/InRelease"))
            manifest_text = (in_release or b"").decode("utf-8", errors="replace")

        entries = filter_index_entries_for_architectures(parse_release_sha256_entries(manifest_text), architectures)
        for entry in entries:
            named_key = dists_object_key(timestamp, archive, f"{suite}/{entry.path}")
            by_hash_key = dists_object_key(timestamp, archive, f"{suite}/{by_hash_path_for_entry(entry)}")
            if self.storage.has_object(named_key) and self.storage.has_object(by_hash_key):
                present_count += 1
                continue
            data = self.fetcher.fetch(f"{snapshot_dists}/{entry.path}")
            if data is None:
                # Release files can list optional members the snapshot did not
                # capture; count and continue so one gap cannot block a cut.
                logger.warning("Release-listed index missing upstream: {}/{}/{}", archive, suite, entry.path)
                missing_count += 1
                continue
            actual_sha256 = hashlib.sha256(data).hexdigest()
            if actual_sha256 != entry.sha256:
                raise AptMirrorChecksumMismatchError(entry.path, entry.sha256, actual_sha256)
            self.storage.put_object(named_key, data)
            self.storage.put_object(by_hash_key, data)
            stored_count += 1
        return _SuiteCutCounts(
            stored_count=stored_count,
            already_present_count=present_count,
            missing_upstream_count=missing_count,
        )

    def resolve_package_names(
        self,
        timestamp: str,
        package_names: Sequence[str],
        architectures: tuple[str, ...],
        suites_by_archive: Mapping[str, tuple[str, ...]],
    ) -> PackageListResolution:
        """Resolve listed package names to pool files via the cut Packages indexes.

        A name may resolve to several files (one per architecture, suite, or
        archive it appears in); all of them are returned. Raises
        AptMirrorNotCutError when a main Packages index is absent for the
        timestamp. Names found in no index are reported, not raised, so one
        typo cannot abort a warm that is otherwise useful.
        """
        validate_snapshot_timestamp(timestamp)
        wanted_names = set(package_names)
        found_names: set[str] = set()
        seen_pool_subpaths: set[str] = set()
        resolved: list[ResolvedPoolFile] = []
        for archive, suites in suites_by_archive.items():
            validate_archive_name(archive)
            for suite in suites:
                for arch in architectures:
                    for component in _COMPONENTS:
                        packages_subpath = f"{suite}/{component}/binary-{arch}/Packages.xz"
                        data = self.storage.get_object(dists_object_key(timestamp, archive, packages_subpath))
                        if data is None:
                            if component == "main":
                                raise AptMirrorNotCutError(
                                    timestamp, dists_object_key(timestamp, archive, packages_subpath)
                                )
                            continue
                        for package_name, filename in parse_packages_name_and_pool_path_pairs(data, packages_subpath):
                            if package_name not in wanted_names or not filename.startswith("pool/"):
                                continue
                            found_names.add(package_name)
                            # Index-declared paths become R2 write keys, so a
                            # traversal-shaped Filename must raise, not warm.
                            pool_subpath = validate_safe_subpath(filename[len("pool/") :])
                            if pool_subpath in seen_pool_subpaths:
                                continue
                            seen_pool_subpaths.add(pool_subpath)
                            resolved.append(
                                ResolvedPoolFile(
                                    archive=archive,
                                    pool_subpath=pool_subpath,
                                    package_name=package_name,
                                )
                            )
        unknown_names = tuple(name for name in package_names if name not in found_names)
        return PackageListResolution(resolved_files=tuple(resolved), unknown_package_names=unknown_names)

    def warm(
        self,
        timestamp: str,
        resolution: PackageListResolution,
        max_workers: int,
    ) -> AptMirrorWarmResult:
        """Fetch every resolved pool file into the cache, in parallel; runs to completion."""
        fetched_count = 0
        cached_count = 0
        missing_paths: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            outcome_futures = {
                pool.submit(self._warm_one, timestamp, resolved_file): resolved_file
                for resolved_file in resolution.resolved_files
            }
            for future in concurrent.futures.as_completed(outcome_futures):
                resolved_file = outcome_futures[future]
                outcome = future.result()
                match outcome:
                    case _WarmOutcome.FETCHED:
                        fetched_count += 1
                    case _WarmOutcome.ALREADY_CACHED:
                        cached_count += 1
                    case _WarmOutcome.MISSING:
                        missing_paths.append(resolved_file.qualified_pool_path)
                    case _ as unreachable:
                        assert_never(unreachable)
        return AptMirrorWarmResult(
            timestamp=timestamp,
            examined_count=len(resolution.resolved_files),
            fetched_count=fetched_count,
            already_cached_count=cached_count,
            missing_pool_paths=tuple(sorted(missing_paths)),
            unknown_package_names=resolution.unknown_package_names,
        )

    def _warm_one(self, timestamp: str, resolved_file: ResolvedPoolFile) -> "_WarmOutcome":
        cache_key = pool_cache_key(resolved_file.archive, resolved_file.pool_subpath)
        if self.storage.has_object(cache_key):
            return _WarmOutcome.ALREADY_CACHED
        try:
            data = self._fetch_pool_file_from_upstreams(timestamp, resolved_file.archive, resolved_file.pool_subpath)
        except AptMirrorObjectNotFoundError:
            logger.warning(
                "Pool file missing on all upstreams: {}/pool/{}", resolved_file.archive, resolved_file.pool_subpath
            )
            return _WarmOutcome.MISSING
        self.storage.put_object(cache_key, data)
        return _WarmOutcome.FETCHED

    def verify(
        self,
        timestamp: str,
        resolution: PackageListResolution,
        max_workers: int,
    ) -> AptMirrorVerifyResult:
        """Read-only check that every resolved pool file is already in the cache."""
        cached_count = 0
        missing_paths: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            presence_futures = {
                pool.submit(
                    self.storage.has_object, pool_cache_key(resolved_file.archive, resolved_file.pool_subpath)
                ): resolved_file
                for resolved_file in resolution.resolved_files
            }
            for future in concurrent.futures.as_completed(presence_futures):
                resolved_file = presence_futures[future]
                if future.result():
                    cached_count += 1
                else:
                    missing_paths.append(resolved_file.qualified_pool_path)
        return AptMirrorVerifyResult(
            timestamp=timestamp,
            cached_count=cached_count,
            missing_pool_paths=tuple(sorted(missing_paths)),
            unknown_package_names=resolution.unknown_package_names,
        )

    def _fetch_pool_file_from_upstreams(self, timestamp: str, archive: str, subpath: str) -> bytes:
        """Fetch a pool file from the live archive, then snapshot.debian.org at the timestamp."""
        upstream_base = self.upstream_by_archive.get(archive)
        if upstream_base is not None:
            live = self.fetcher.fetch(f"{upstream_base}/pool/{subpath}")
            if live is not None:
                return live
        snapshot_url = f"{self.snapshot_base}/{archive}/{timestamp}/pool/{subpath}"
        from_snapshot = self.fetcher.fetch(snapshot_url)
        if from_snapshot is None:
            raise AptMirrorObjectNotFoundError(f"{archive}/pool/{subpath}")
        return from_snapshot
