import hashlib

import pytest

from imbue.apt_mirror.data_types import AptMirrorCutRequest
from imbue.apt_mirror.errors import AptMirrorChecksumMismatchError
from imbue.apt_mirror.errors import AptMirrorNotCutError
from imbue.apt_mirror.errors import AptMirrorObjectNotFoundError
from imbue.apt_mirror.errors import AptMirrorUnsafePathError
from imbue.apt_mirror.mock_apt_mirror_test import InMemoryAptMirrorStorage
from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher
from imbue.apt_mirror.parsing import dists_object_key
from imbue.apt_mirror.parsing import pool_cache_key
from imbue.apt_mirror.service import AptMirrorService
from imbue.apt_mirror.testing import FOO_AND_BAR_PACKAGES_TEXT
from imbue.apt_mirror.testing import FOO_PACKAGES_TEXT
from imbue.apt_mirror.testing import compress_packages_index
from imbue.apt_mirror.testing import wire_canned_suite

TIMESTAMP = "20260725T000000Z"
SNAPSHOT_DEBIAN = f"https://snapshot.debian.org/archive/debian/{TIMESTAMP}"
_SUITES: dict[str, tuple[str, ...]] = {"debian": ("trixie",)}
_ARCHES = ("amd64",)


def _make_service(
    storage: InMemoryAptMirrorStorage | None = None,
    fetcher: MappingUpstreamFetcher | None = None,
) -> AptMirrorService:
    return AptMirrorService(
        storage=storage if storage is not None else InMemoryAptMirrorStorage(),
        fetcher=fetcher if fetcher is not None else MappingUpstreamFetcher(),
    )


def _canned_suite(fetcher: MappingUpstreamFetcher, packages_text: str) -> bytes:
    """Wire a minimal single-suite archive at TIMESTAMP into the fetcher; returns the Packages.xz bytes."""
    packages_xz = compress_packages_index(packages_text)
    wire_canned_suite(fetcher, SNAPSHOT_DEBIAN, "trixie", {"amd64": packages_xz})
    return packages_xz


def _cut_request() -> AptMirrorCutRequest:
    return AptMirrorCutRequest(timestamp=TIMESTAMP, architectures=_ARCHES, suites_by_archive=_SUITES)


# ---------------------------------------------------------------------------
# Cut


def test_cut_freezes_indexes_with_by_hash_aliases() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    packages_xz = _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    service = _make_service(storage=storage, fetcher=fetcher)

    result = service.cut(_cut_request())

    assert result.missing_upstream_count == 0
    assert storage.get_object(dists_object_key(TIMESTAMP, "debian", "trixie/InRelease")) is not None
    stored_packages = storage.get_object(dists_object_key(TIMESTAMP, "debian", "trixie/main/binary-amd64/Packages.xz"))
    assert stored_packages == packages_xz
    packages_sha = hashlib.sha256(packages_xz).hexdigest()
    by_hash_key = dists_object_key(TIMESTAMP, "debian", f"trixie/main/binary-amd64/by-hash/SHA256/{packages_sha}")
    assert storage.get_object(by_hash_key) == packages_xz


def test_cut_is_idempotent() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    service = _make_service(storage=storage, fetcher=fetcher)

    first = service.cut(_cut_request())
    puts_after_first = storage.put_count
    second = service.cut(_cut_request())

    assert first.stored_index_count > 0
    assert second.stored_index_count == 0
    assert second.already_present_count > 0
    assert storage.put_count == puts_after_first


def test_cut_checksum_mismatch_raises() -> None:
    """An index whose bytes do not match the Release-declared sha256 aborts the cut."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    fetcher.responses_by_url[f"{SNAPSHOT_DEBIAN}/dists/trixie/main/binary-amd64/Packages.xz"] = b"corrupted"
    service = _make_service(storage=storage, fetcher=fetcher)

    with pytest.raises(AptMirrorChecksumMismatchError):
        service.cut(_cut_request())


def test_cut_without_detached_release_uses_in_release_manifest() -> None:
    """The absent Release/Release.gpg pair is counted, and the manifest is read from InRelease."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    packages_xz = _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    del fetcher.responses_by_url[f"{SNAPSHOT_DEBIAN}/dists/trixie/Release"]
    del fetcher.responses_by_url[f"{SNAPSHOT_DEBIAN}/dists/trixie/Release.gpg"]
    service = _make_service(storage=storage, fetcher=fetcher)

    result = service.cut(_cut_request())

    assert result.missing_upstream_count == 2
    stored_packages = storage.get_object(dists_object_key(TIMESTAMP, "debian", "trixie/main/binary-amd64/Packages.xz"))
    assert stored_packages == packages_xz


def test_cut_tolerates_release_listed_index_missing_upstream() -> None:
    """A Release-listed index the snapshot did not capture is counted, not fatal."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    del fetcher.responses_by_url[f"{SNAPSHOT_DEBIAN}/dists/trixie/main/binary-amd64/Packages.xz"]
    service = _make_service(storage=storage, fetcher=fetcher)

    result = service.cut(_cut_request())

    assert result.missing_upstream_count == 1
    assert storage.get_object(dists_object_key(TIMESTAMP, "debian", "trixie/InRelease")) is not None


def test_cut_missing_in_release_raises() -> None:
    service = _make_service()
    with pytest.raises(AptMirrorObjectNotFoundError):
        service.cut(_cut_request())


def test_cut_rejects_unsafe_archive_name() -> None:
    """A malformed archive name in the request raises before anything is fetched."""
    service = _make_service()
    request = AptMirrorCutRequest(
        timestamp=TIMESTAMP,
        architectures=_ARCHES,
        suites_by_archive={"../evil": ("trixie",)},
    )
    with pytest.raises(AptMirrorUnsafePathError):
        service.cut(request)


# ---------------------------------------------------------------------------
# Resolution


def test_resolve_package_names_maps_names_to_pool_files_and_reports_unknown() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_AND_BAR_PACKAGES_TEXT)
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())

    resolution = service.resolve_package_names(
        timestamp=TIMESTAMP,
        package_names=["foo", "no-such-package"],
        architectures=_ARCHES,
        suites_by_archive=_SUITES,
    )

    assert [f.pool_subpath for f in resolution.resolved_files] == ["main/f/foo/foo_1.0_amd64.deb"]
    assert resolution.resolved_files[0].package_name == "foo"
    assert resolution.unknown_package_names == ("no-such-package",)


def test_resolve_package_names_before_cut_raises() -> None:
    service = _make_service()
    with pytest.raises(AptMirrorNotCutError):
        service.resolve_package_names(
            timestamp=TIMESTAMP,
            package_names=["foo"],
            architectures=_ARCHES,
            suites_by_archive=_SUITES,
        )


def test_resolve_package_names_deduplicates_shared_pool_files() -> None:
    """An arch-independent package listed under several arches resolves to one pool file."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    packages_text = "Package: fonts-foo\nFilename: pool/main/f/fonts-foo/fonts-foo_1.0_all.deb\n"
    packages_xz = compress_packages_index(packages_text)
    for arch in ("amd64", "arm64"):
        storage.put_object(
            dists_object_key(TIMESTAMP, "debian", f"trixie/main/binary-{arch}/Packages.xz"), packages_xz
        )
    service = _make_service(storage=storage, fetcher=fetcher)

    resolution = service.resolve_package_names(
        timestamp=TIMESTAMP,
        package_names=["fonts-foo"],
        architectures=("amd64", "arm64"),
        suites_by_archive=_SUITES,
    )

    assert [f.pool_subpath for f in resolution.resolved_files] == ["main/f/fonts-foo/fonts-foo_1.0_all.deb"]
    assert resolution.unknown_package_names == ()


def test_resolve_package_names_rejects_traversal_pool_paths() -> None:
    """A traversal-shaped Filename in an index raises instead of becoming an R2 write key."""
    storage = InMemoryAptMirrorStorage()
    packages_text = "Package: evil\nFilename: pool/../../snap/x\n"
    storage.put_object(
        dists_object_key(TIMESTAMP, "debian", "trixie/main/binary-amd64/Packages.xz"),
        compress_packages_index(packages_text),
    )
    service = _make_service(storage=storage)

    with pytest.raises(AptMirrorUnsafePathError):
        service.resolve_package_names(
            timestamp=TIMESTAMP,
            package_names=["evil"],
            architectures=_ARCHES,
            suites_by_archive=_SUITES,
        )


# ---------------------------------------------------------------------------
# Warm


def test_warm_fetches_listed_pool_files_from_both_upstreams() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_AND_BAR_PACKAGES_TEXT)
    fetcher.responses_by_url["https://deb.debian.org/debian/pool/main/f/foo/foo_1.0_amd64.deb"] = b"foo-deb"
    fetcher.responses_by_url[f"{SNAPSHOT_DEBIAN}/pool/main/b/bar/bar_2.0_amd64.deb"] = b"bar-deb"
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = service.resolve_package_names(
        timestamp=TIMESTAMP, package_names=["foo", "bar"], architectures=_ARCHES, suites_by_archive=_SUITES
    )

    result = service.warm(TIMESTAMP, resolution, max_workers=2)

    assert result.is_complete
    assert result.fetched_count == 2
    assert result.missing_pool_paths == ()
    assert storage.get_object(pool_cache_key("debian", "main/f/foo/foo_1.0_amd64.deb")) == b"foo-deb"
    assert storage.get_object(pool_cache_key("debian", "main/b/bar/bar_2.0_amd64.deb")) == b"bar-deb"


def test_warm_skips_already_cached_files() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    storage.put_object(pool_cache_key("debian", "main/f/foo/foo_1.0_amd64.deb"), b"already")
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = service.resolve_package_names(
        timestamp=TIMESTAMP, package_names=["foo"], architectures=_ARCHES, suites_by_archive=_SUITES
    )

    result = service.warm(TIMESTAMP, resolution, max_workers=2)

    assert result.is_complete
    assert result.already_cached_count == 1
    assert result.fetched_count == 0


def test_warm_reports_files_missing_on_all_upstreams_as_incomplete() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = service.resolve_package_names(
        timestamp=TIMESTAMP, package_names=["foo"], architectures=_ARCHES, suites_by_archive=_SUITES
    )

    result = service.warm(TIMESTAMP, resolution, max_workers=2)

    assert not result.is_complete
    assert result.missing_pool_paths == ("debian/pool/main/f/foo/foo_1.0_amd64.deb",)
    assert result.fetched_count == 0


def test_warm_with_unknown_package_names_is_incomplete() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    fetcher.responses_by_url["https://deb.debian.org/debian/pool/main/f/foo/foo_1.0_amd64.deb"] = b"foo-deb"
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = service.resolve_package_names(
        timestamp=TIMESTAMP, package_names=["foo", "typo-name"], architectures=_ARCHES, suites_by_archive=_SUITES
    )

    result = service.warm(TIMESTAMP, resolution, max_workers=2)

    assert not result.is_complete
    assert result.unknown_package_names == ("typo-name",)
    assert result.fetched_count == 1


# ---------------------------------------------------------------------------
# Verify


def test_verify_reports_cached_and_missing_without_fetching() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_AND_BAR_PACKAGES_TEXT)
    storage.put_object(pool_cache_key("debian", "main/f/foo/foo_1.0_amd64.deb"), b"foo-deb")
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = service.resolve_package_names(
        timestamp=TIMESTAMP, package_names=["foo", "bar"], architectures=_ARCHES, suites_by_archive=_SUITES
    )
    fetcher.fetched_urls.clear()

    result = service.verify(TIMESTAMP, resolution, max_workers=2)

    assert result.cached_count == 1
    assert result.missing_pool_paths == ("debian/pool/main/b/bar/bar_2.0_amd64.deb",)
    assert not result.is_complete
    assert fetcher.fetched_urls == []
