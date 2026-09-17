"""Cut/warm/verify operations for the snapshot-pinned apt mirror.

The mirror's serve path is the Cloudflare Worker in ``worker/`` reading the
same R2 bucket; this service only writes to the bucket (freezing index sets
and pre-fetching package files), so it runs wherever the operator runs the CLI.
"""

import concurrent.futures
import hashlib
from collections.abc import Sequence
from enum import auto
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.apt_mirror.data_types import AptMirrorCutRequest
from imbue.apt_mirror.data_types import AptMirrorCutResult
from imbue.apt_mirror.data_types import AptMirrorVerifyResult
from imbue.apt_mirror.data_types import AptMirrorWarmResult
from imbue.apt_mirror.data_types import ArchiveSource
from imbue.apt_mirror.data_types import PackageListResolution
from imbue.apt_mirror.data_types import PackageSpec
from imbue.apt_mirror.data_types import PackagesIndexEntry
from imbue.apt_mirror.data_types import ResolvedPackageFile
from imbue.apt_mirror.errors import AptMirrorChecksumMismatchError
from imbue.apt_mirror.errors import AptMirrorNotCutError
from imbue.apt_mirror.errors import AptMirrorObjectNotFoundError
from imbue.apt_mirror.interfaces import AptMirrorStorageInterface
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface
from imbue.apt_mirror.parsing import PACKAGES_INDEX_NAMES
from imbue.apt_mirror.parsing import by_hash_path_for_entry
from imbue.apt_mirror.parsing import dists_object_key
from imbue.apt_mirror.parsing import filter_index_entries_for_architectures
from imbue.apt_mirror.parsing import filter_index_entries_for_components
from imbue.apt_mirror.parsing import package_file_object_key
from imbue.apt_mirror.parsing import parse_packages_index_entries
from imbue.apt_mirror.parsing import parse_release_sha256_entries
from imbue.apt_mirror.parsing import select_newest_entry
from imbue.apt_mirror.parsing import validate_archive_name
from imbue.apt_mirror.parsing import validate_package_filename
from imbue.apt_mirror.parsing import validate_snapshot_timestamp
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel


class _SuiteCutCounts(FrozenModel):
    """Index-object counts from freezing one suite."""

    stored_count: int = Field(description="Index objects newly stored in the bucket")
    already_present_count: int = Field(description="Index objects that were already stored")
    missing_upstream_count: int = Field(description="Files the index source did not serve")


class _WarmOutcome(UpperCaseStrEnum):
    """What happened to a single package file during a warm pass."""

    FETCHED = auto()
    ALREADY_CACHED = auto()
    MISSING = auto()


def _index_source_base(archive: ArchiveSource, timestamp: str) -> str:
    """The archive root a cut freezes indexes from: the snapshot at the timestamp when one exists, else live."""
    snapshot_root = archive.snapshot_root(timestamp)
    return snapshot_root if snapshot_root is not None else archive.live_base


class AptMirrorService(MutableModel):
    """Writes frozen index sets and pre-fetched package files into the mirror bucket."""

    storage: AptMirrorStorageInterface = Field(frozen=True, description="The mirror bucket")
    fetcher: UpstreamFetcherInterface = Field(frozen=True, description="HTTP fetcher for upstream archives")

    def cut(self, request: AptMirrorCutRequest) -> AptMirrorCutResult:
        """Freeze the index set for a timestamp into the bucket. Idempotent."""
        timestamp = validate_snapshot_timestamp(request.timestamp)
        stored_count = 0
        present_count = 0
        missing_count = 0
        for archive in request.archives:
            validate_archive_name(archive.name)
            for suite in archive.suites:
                with log_span("Freezing indexes for {}/{} at {}", archive.name, suite, timestamp):
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
        archive: ArchiveSource,
        suite: str,
        architectures: tuple[str, ...],
    ) -> _SuiteCutCounts:
        """Freeze one suite's indexes into the bucket."""
        source_dists = f"{_index_source_base(archive, timestamp)}/dists/{suite}"
        stored_count = 0
        present_count = 0
        missing_count = 0

        # The signed entry points come first: InRelease (inline-signed) is
        # mandatory; the detached Release/Release.gpg pair is stored when
        # present so older apt configurations keep working.
        in_release_key = dists_object_key(timestamp, archive.name, f"{suite}/InRelease")
        for name in ("InRelease", "Release", "Release.gpg"):
            data = self.fetcher.fetch(f"{source_dists}/{name}")
            if data is None:
                if name == "InRelease":
                    raise AptMirrorObjectNotFoundError(f"{archive.name}/dists/{suite}/InRelease at {timestamp}")
                missing_count += 1
                continue
            key = dists_object_key(timestamp, archive.name, f"{suite}/{name}")
            if self.storage.has_object(key):
                present_count += 1
            else:
                self.storage.put_object(key, data)
                stored_count += 1

        # The manifest is the InRelease the bucket holds, not the one just
        # fetched: an archive without a snapshot service moves on between
        # runs, so a re-cut must freeze against the entry point the first run
        # stored or it would replace an index under a signature that names a
        # different hash. The clearsigned payload carries the same SHA256
        # section as the detached Release, which the parser reads regardless
        # of signature armor.
        stored_in_release = self.storage.get_object(in_release_key)
        if stored_in_release is None:
            raise AptMirrorObjectNotFoundError(in_release_key)
        manifest_text = stored_in_release.decode("utf-8", errors="replace")

        entries = filter_index_entries_for_components(
            filter_index_entries_for_architectures(parse_release_sha256_entries(manifest_text), architectures),
            archive.components,
        )
        for entry in entries:
            named_key = dists_object_key(timestamp, archive.name, f"{suite}/{entry.path}")
            by_hash_key = dists_object_key(timestamp, archive.name, f"{suite}/{by_hash_path_for_entry(entry)}")
            if self.storage.has_object(named_key) and self.storage.has_object(by_hash_key):
                present_count += 1
                continue
            data = self.fetcher.fetch(f"{source_dists}/{entry.path}")
            if data is None:
                # Release files can list optional members the source does not
                # serve; count and continue so one gap cannot block a cut.
                logger.warning("Release-listed index missing upstream: {}/{}/{}", archive.name, suite, entry.path)
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

    def _read_cut_packages_index(
        self, timestamp: str, archive: ArchiveSource, suite: str, component: str, arch: str
    ) -> list[PackagesIndexEntry] | None:
        """The parsed Packages index a cut froze for one (suite, component, arch), or None when none was stored."""
        for index_name in PACKAGES_INDEX_NAMES:
            subpath = f"{suite}/{component}/binary-{arch}/{index_name}"
            data = self.storage.get_object(dists_object_key(timestamp, archive.name, subpath))
            if data is not None:
                return parse_packages_index_entries(data, subpath)
        return None

    def resolve_package_specs(
        self,
        timestamp: str,
        package_specs: Sequence[PackageSpec],
        architectures: tuple[str, ...],
        archives: Sequence[ArchiveSource],
    ) -> PackageListResolution:
        """Resolve listed package specs to package files via the cut Packages indexes.

        An unversioned spec resolves to the newest version of the package in
        each index it appears in (what ``apt-get install`` would pick against
        that frozen index); a ``name=version`` spec resolves to exactly that
        version. A spec may resolve to several files (one per architecture,
        suite, or archive); all of them are returned. Raises
        AptMirrorNotCutError when an archive's required component has no
        Packages index for the timestamp. Specs found in no index are
        reported, not raised, so one typo cannot abort a warm that is
        otherwise useful.
        """
        validate_snapshot_timestamp(timestamp)
        spec_by_text = {spec.text: spec for spec in package_specs}
        found_spec_texts: set[str] = set()
        seen_filenames: set[str] = set()
        resolved: list[ResolvedPackageFile] = []
        for archive in archives:
            validate_archive_name(archive.name)
            for suite in archive.suites:
                for arch in architectures:
                    for component in archive.components:
                        entries = self._read_cut_packages_index(timestamp, archive, suite, component, arch)
                        if entries is None:
                            if component == archive.required_component:
                                missing_key = dists_object_key(
                                    timestamp, archive.name, f"{suite}/{component}/binary-{arch}/Packages"
                                )
                                raise AptMirrorNotCutError(timestamp, missing_key)
                            continue
                        entries_by_name = _group_entries_by_package_name(entries)
                        for spec in spec_by_text.values():
                            for entry in _select_entries_for_spec(entries_by_name.get(spec.name, ()), spec):
                                found_spec_texts.add(spec.text)
                                filename = validate_package_filename(entry.filename)
                                if filename in seen_filenames:
                                    continue
                                seen_filenames.add(filename)
                                resolved.append(
                                    ResolvedPackageFile(
                                        archive=archive.name, filename=filename, package_name=spec.name
                                    )
                                )
        unresolved = tuple(spec.text for spec in package_specs if spec.text not in found_spec_texts)
        return PackageListResolution(resolved_files=tuple(resolved), unresolved_specs=unresolved)

    def warm(
        self,
        timestamp: str,
        resolution: PackageListResolution,
        archives: Sequence[ArchiveSource],
        max_workers: int,
    ) -> AptMirrorWarmResult:
        """Fetch every resolved package file into the bucket, in parallel; runs to completion."""
        archive_by_name = {archive.name: archive for archive in archives}
        fetched_count = 0
        cached_count = 0
        missing_paths: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            outcome_futures = {
                pool.submit(self._warm_one, timestamp, archive_by_name[resolved_file.archive], resolved_file): (
                    resolved_file
                )
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
                        missing_paths.append(resolved_file.qualified_path)
                    case _ as unreachable:
                        assert_never(unreachable)
        return AptMirrorWarmResult(
            timestamp=timestamp,
            examined_count=len(resolution.resolved_files),
            fetched_count=fetched_count,
            already_cached_count=cached_count,
            missing_paths=tuple(sorted(missing_paths)),
            unresolved_specs=resolution.unresolved_specs,
        )

    def _warm_one(self, timestamp: str, archive: ArchiveSource, resolved_file: ResolvedPackageFile) -> "_WarmOutcome":
        key = package_file_object_key(timestamp, archive.name, resolved_file.filename)
        if self.storage.has_object(key):
            return _WarmOutcome.ALREADY_CACHED
        try:
            data = self._fetch_package_file_from_upstreams(timestamp, archive, resolved_file.filename)
        except AptMirrorObjectNotFoundError:
            logger.warning("Package file missing on all upstreams: {}", resolved_file.qualified_path)
            return _WarmOutcome.MISSING
        self.storage.put_object(key, data)
        return _WarmOutcome.FETCHED

    def verify(
        self,
        timestamp: str,
        resolution: PackageListResolution,
        max_workers: int,
    ) -> AptMirrorVerifyResult:
        """Read-only check that every resolved package file is already in the bucket."""
        cached_count = 0
        missing_paths: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            presence_futures = {
                pool.submit(
                    self.storage.has_object,
                    package_file_object_key(timestamp, resolved_file.archive, resolved_file.filename),
                ): resolved_file
                for resolved_file in resolution.resolved_files
            }
            for future in concurrent.futures.as_completed(presence_futures):
                resolved_file = presence_futures[future]
                if future.result():
                    cached_count += 1
                else:
                    missing_paths.append(resolved_file.qualified_path)
        return AptMirrorVerifyResult(
            timestamp=timestamp,
            cached_count=cached_count,
            missing_paths=tuple(sorted(missing_paths)),
            unresolved_specs=resolution.unresolved_specs,
        )

    def _fetch_package_file_from_upstreams(self, timestamp: str, archive: ArchiveSource, filename: str) -> bytes:
        """Fetch a package file from the live archive, then (when the archive has one) the snapshot at the timestamp."""
        live = self.fetcher.fetch(f"{archive.live_base}/{filename}")
        if live is not None:
            return live
        snapshot_root = archive.snapshot_root(timestamp)
        if snapshot_root is not None:
            from_snapshot = self.fetcher.fetch(f"{snapshot_root}/{filename}")
            if from_snapshot is not None:
                return from_snapshot
        raise AptMirrorObjectNotFoundError(f"{archive.name}/{filename}")


def _group_entries_by_package_name(entries: Sequence[PackagesIndexEntry]) -> dict[str, list[PackagesIndexEntry]]:
    entries_by_name: dict[str, list[PackagesIndexEntry]] = {}
    for entry in entries:
        entries_by_name.setdefault(entry.package_name, []).append(entry)
    return entries_by_name


def _select_entries_for_spec(candidates: Sequence[PackagesIndexEntry], spec: PackageSpec) -> list[PackagesIndexEntry]:
    """The entries a spec names among one package's index entries: the exact version when pinned, else the single newest."""
    if not candidates:
        return []
    if spec.version is not None:
        return [entry for entry in candidates if entry.version == spec.version]
    return [select_newest_entry(candidates)]
