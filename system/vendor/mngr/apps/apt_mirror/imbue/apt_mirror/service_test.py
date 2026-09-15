import gzip
import hashlib

import pytest

from imbue.apt_mirror.data_types import AptMirrorCutRequest
from imbue.apt_mirror.data_types import ArchiveSource
from imbue.apt_mirror.data_types import PackageListResolution
from imbue.apt_mirror.data_types import PackageSpec
from imbue.apt_mirror.errors import AptMirrorChecksumMismatchError
from imbue.apt_mirror.errors import AptMirrorNotCutError
from imbue.apt_mirror.errors import AptMirrorObjectNotFoundError
from imbue.apt_mirror.errors import AptMirrorUnsafePathError
from imbue.apt_mirror.mock_apt_mirror_test import InMemoryAptMirrorStorage
from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher
from imbue.apt_mirror.package_lists import parse_package_spec
from imbue.apt_mirror.parsing import dists_object_key
from imbue.apt_mirror.parsing import pool_cache_key
from imbue.apt_mirror.service import AptMirrorService
from imbue.apt_mirror.testing import FOO_AND_BAR_PACKAGES_TEXT
from imbue.apt_mirror.testing import FOO_PACKAGES_TEXT
from imbue.apt_mirror.testing import TEST_DEBIAN_ARCHIVE
from imbue.apt_mirror.testing import compress_packages_index
from imbue.apt_mirror.testing import wire_canned_suite

TIMESTAMP = "20260725T000000Z"
SNAPSHOT_DEBIAN = f"https://snapshot.debian.org/archive/debian/{TIMESTAMP}"
_ARCHIVES = (TEST_DEBIAN_ARCHIVE,)
_ARCHES = ("amd64",)

# A Docker-style archive: no snapshot service, a ``stable`` component, and
# package files nested under dists/<suite>/pool/.
_DOCKER_LIVE = "https://docker.example.test/linux/debian"
_DOCKER_ARCHIVE = ArchiveSource(
    name="docker", live_base=_DOCKER_LIVE, snapshot_base=None, suites=("trixie",), components=("stable",)
)
_DOCKER_PACKAGES_TEXT = (
    "Package: docker-ce\nVersion: 5:29.6.2-1~debian.13~trixie\n"
    "Filename: dists/trixie/pool/stable/amd64/docker-ce_29.6.2-1~debian.13~trixie_amd64.deb\n\n"
    "Package: docker-ce\nVersion: 5:29.8.0-1~debian.13~trixie\n"
    "Filename: dists/trixie/pool/stable/amd64/docker-ce_29.8.0-1~debian.13~trixie_amd64.deb\n\n"
    "Package: docker-buildx-plugin\nVersion: 0.9.0-1~debian.13~trixie\n"
    "Filename: dists/trixie/pool/stable/amd64/docker-buildx-plugin_0.9.0-1~debian.13~trixie_amd64.deb\n\n"
    "Package: docker-buildx-plugin\nVersion: 0.37.0-1~debian.13~trixie\n"
    "Filename: dists/trixie/pool/stable/amd64/docker-buildx-plugin_0.37.0-1~debian.13~trixie_amd64.deb\n"
)


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


def _cut_request(archives: tuple[ArchiveSource, ...] = _ARCHIVES) -> AptMirrorCutRequest:
    return AptMirrorCutRequest(timestamp=TIMESTAMP, architectures=_ARCHES, archives=archives)


def _specs(*texts: str) -> list[PackageSpec]:
    return [parse_package_spec(text) for text in texts]


def _resolve(
    service: AptMirrorService, *texts: str, archives: tuple[ArchiveSource, ...] = _ARCHIVES
) -> PackageListResolution:
    return service.resolve_package_specs(
        timestamp=TIMESTAMP, package_specs=_specs(*texts), architectures=_ARCHES, archives=archives
    )


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


def test_cut_skips_indexes_outside_the_configured_components() -> None:
    """Only the archive's configured components are frozen (Docker lists edge/test/nightly too)."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    stable_xz = compress_packages_index(_DOCKER_PACKAGES_TEXT)
    nightly_xz = compress_packages_index("Package: nightly-only\nVersion: 1\nFilename: dists/trixie/pool/x.deb\n")
    wire_canned_suite(fetcher, _DOCKER_LIVE, "trixie", {"amd64": stable_xz}, component="stable")
    release_url = f"{_DOCKER_LIVE}/dists/trixie/InRelease"
    nightly_line = f" {hashlib.sha256(nightly_xz).hexdigest()} {len(nightly_xz)} nightly/binary-amd64/Packages.xz\n"
    fetcher.responses_by_url[release_url] = fetcher.responses_by_url[release_url] + nightly_line.encode()
    fetcher.responses_by_url[f"{_DOCKER_LIVE}/dists/trixie/Release"] = fetcher.responses_by_url[release_url]
    fetcher.responses_by_url[f"{_DOCKER_LIVE}/dists/trixie/nightly/binary-amd64/Packages.xz"] = nightly_xz
    service = _make_service(storage=storage, fetcher=fetcher)

    service.cut(_cut_request(archives=(_DOCKER_ARCHIVE,)))

    assert storage.has_object(dists_object_key(TIMESTAMP, "docker", "trixie/stable/binary-amd64/Packages.xz"))
    assert not storage.has_object(dists_object_key(TIMESTAMP, "docker", "trixie/nightly/binary-amd64/Packages.xz"))
    assert f"{_DOCKER_LIVE}/dists/trixie/nightly/binary-amd64/Packages.xz" not in fetcher.fetched_urls


def test_cut_of_a_snapshotless_archive_freezes_its_live_indexes() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    wire_canned_suite(
        fetcher, _DOCKER_LIVE, "trixie", {"amd64": compress_packages_index(_DOCKER_PACKAGES_TEXT)}, component="stable"
    )
    service = _make_service(storage=storage, fetcher=fetcher)

    result = service.cut(_cut_request(archives=(_DOCKER_ARCHIVE,)))

    assert result.missing_upstream_count == 0
    assert storage.has_object(dists_object_key(TIMESTAMP, "docker", "trixie/InRelease"))
    assert all(url.startswith(_DOCKER_LIVE) for url in fetcher.fetched_urls)


def test_recut_of_a_snapshotless_archive_keeps_the_indexes_the_first_cut_froze() -> None:
    """Docker's live repo moves on between runs; a re-cut must not replace a frozen index under the stored InRelease."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    first_xz = compress_packages_index(_DOCKER_PACKAGES_TEXT)
    wire_canned_suite(fetcher, _DOCKER_LIVE, "trixie", {"amd64": first_xz}, component="stable")
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request(archives=(_DOCKER_ARCHIVE,)))
    in_release_key = dists_object_key(TIMESTAMP, "docker", "trixie/InRelease")
    packages_key = dists_object_key(TIMESTAMP, "docker", "trixie/stable/binary-amd64/Packages.xz")
    first_in_release = storage.get_object(in_release_key)
    newer_xz = compress_packages_index(
        _DOCKER_PACKAGES_TEXT
        + "\nPackage: docker-model-plugin\nVersion: 1.0\nFilename: dists/trixie/pool/stable/amd64/m.deb\n"
    )
    wire_canned_suite(fetcher, _DOCKER_LIVE, "trixie", {"amd64": newer_xz}, component="stable")
    fetcher.fetched_urls.clear()

    result = service.cut(_cut_request(archives=(_DOCKER_ARCHIVE,)))

    assert result.stored_index_count == 0
    assert storage.get_object(in_release_key) == first_in_release
    assert storage.get_object(packages_key) == first_xz
    assert f"{_DOCKER_LIVE}/dists/trixie/stable/binary-amd64/Packages.xz" not in fetcher.fetched_urls


def test_cut_missing_in_release_raises() -> None:
    service = _make_service()
    with pytest.raises(AptMirrorObjectNotFoundError):
        service.cut(_cut_request())


def test_cut_rejects_unsafe_archive_name() -> None:
    """A malformed archive name in the request raises before anything is fetched."""
    service = _make_service()
    evil = ArchiveSource(
        name="../evil", live_base="https://x", snapshot_base=None, suites=("trixie",), components=("main",)
    )
    with pytest.raises(AptMirrorUnsafePathError):
        service.cut(_cut_request(archives=(evil,)))


# ---------------------------------------------------------------------------
# Resolution


def test_resolve_package_specs_maps_names_to_package_files_and_reports_unknown() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_AND_BAR_PACKAGES_TEXT)
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())

    resolution = _resolve(service, "foo", "no-such-package")

    assert [f.filename for f in resolution.resolved_files] == ["pool/main/f/foo/foo_1.0_amd64.deb"]
    assert resolution.resolved_files[0].package_name == "foo"
    assert resolution.unresolved_specs == ("no-such-package",)


def test_resolve_package_specs_before_cut_raises() -> None:
    service = _make_service()
    with pytest.raises(AptMirrorNotCutError):
        _resolve(service, "foo")


def test_resolve_package_specs_reads_a_gzip_only_index() -> None:
    """Docker publishes no Packages.xz; resolution falls back to the .gz spelling a cut froze."""
    storage = InMemoryAptMirrorStorage()
    storage.put_object(
        dists_object_key(TIMESTAMP, "docker", "trixie/stable/binary-amd64/Packages.gz"),
        gzip.compress(_DOCKER_PACKAGES_TEXT.encode()),
    )
    service = _make_service(storage=storage)

    resolution = _resolve(service, "docker-buildx-plugin", archives=(_DOCKER_ARCHIVE,))

    assert [f.filename for f in resolution.resolved_files] == [
        "dists/trixie/pool/stable/amd64/docker-buildx-plugin_0.37.0-1~debian.13~trixie_amd64.deb"
    ]


def test_resolve_package_specs_pins_exact_versions_and_picks_the_newest_otherwise() -> None:
    """``name=version`` resolves to that version only; a bare name to the highest Debian version in the index."""
    storage = InMemoryAptMirrorStorage()
    storage.put_object(
        dists_object_key(TIMESTAMP, "docker", "trixie/stable/binary-amd64/Packages.xz"),
        compress_packages_index(_DOCKER_PACKAGES_TEXT),
    )
    service = _make_service(storage=storage)

    resolution = _resolve(
        service,
        "docker-ce=5:29.6.2-1~debian.13~trixie",
        "docker-buildx-plugin",
        "docker-ce=5:1.0-missing",
        archives=(_DOCKER_ARCHIVE,),
    )

    assert [f.filename for f in resolution.resolved_files] == [
        "dists/trixie/pool/stable/amd64/docker-ce_29.6.2-1~debian.13~trixie_amd64.deb",
        "dists/trixie/pool/stable/amd64/docker-buildx-plugin_0.37.0-1~debian.13~trixie_amd64.deb",
    ]
    assert resolution.unresolved_specs == ("docker-ce=5:1.0-missing",)


def test_resolve_package_specs_orders_versions_the_dpkg_way() -> None:
    """An epoch outranks a numerically larger upstream version (``1:2.0`` > ``9.9``)."""
    storage = InMemoryAptMirrorStorage()
    packages_text = (
        "Package: foo\nVersion: 9.9\nFilename: pool/main/f/foo/foo_9.9_amd64.deb\n\n"
        "Package: foo\nVersion: 1:2.0\nFilename: pool/main/f/foo/foo_2.0_amd64.deb\n"
    )
    storage.put_object(
        dists_object_key(TIMESTAMP, "debian", "trixie/main/binary-amd64/Packages.xz"),
        compress_packages_index(packages_text),
    )
    service = _make_service(storage=storage)

    resolution = _resolve(service, "foo")

    assert [f.filename for f in resolution.resolved_files] == ["pool/main/f/foo/foo_2.0_amd64.deb"]


def test_resolve_package_specs_deduplicates_shared_package_files() -> None:
    """An arch-independent package listed under several arches resolves to one package file."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    packages_text = "Package: fonts-foo\nVersion: 1.0\nFilename: pool/main/f/fonts-foo/fonts-foo_1.0_all.deb\n"
    packages_xz = compress_packages_index(packages_text)
    for arch in ("amd64", "arm64"):
        storage.put_object(
            dists_object_key(TIMESTAMP, "debian", f"trixie/main/binary-{arch}/Packages.xz"), packages_xz
        )
    service = _make_service(storage=storage, fetcher=fetcher)

    resolution = service.resolve_package_specs(
        timestamp=TIMESTAMP,
        package_specs=_specs("fonts-foo"),
        architectures=("amd64", "arm64"),
        archives=_ARCHIVES,
    )

    assert [f.filename for f in resolution.resolved_files] == ["pool/main/f/fonts-foo/fonts-foo_1.0_all.deb"]
    assert resolution.unresolved_specs == ()


def test_resolve_package_specs_rejects_traversal_and_unrooted_filenames() -> None:
    """A traversal-shaped or unexpectedly-rooted Filename raises instead of becoming an R2 write key."""
    for filename in ("pool/../../snap/x", "etc/passwd"):
        storage = InMemoryAptMirrorStorage()
        packages_text = f"Package: evil\nVersion: 1\nFilename: {filename}\n"
        storage.put_object(
            dists_object_key(TIMESTAMP, "debian", "trixie/main/binary-amd64/Packages.xz"),
            compress_packages_index(packages_text),
        )
        service = _make_service(storage=storage)

        with pytest.raises(AptMirrorUnsafePathError):
            _resolve(service, "evil")


# ---------------------------------------------------------------------------
# Warm


def test_warm_fetches_listed_package_files_from_both_upstreams() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_AND_BAR_PACKAGES_TEXT)
    fetcher.responses_by_url["https://deb.debian.org/debian/pool/main/f/foo/foo_1.0_amd64.deb"] = b"foo-deb"
    fetcher.responses_by_url[f"{SNAPSHOT_DEBIAN}/pool/main/b/bar/bar_2.0_amd64.deb"] = b"bar-deb"
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = _resolve(service, "foo", "bar")

    result = service.warm(TIMESTAMP, resolution, _ARCHIVES, max_workers=2)

    assert result.is_complete
    assert result.fetched_count == 2
    assert result.missing_paths == ()
    assert storage.get_object(pool_cache_key("debian", "main/f/foo/foo_1.0_amd64.deb")) == b"foo-deb"
    assert storage.get_object(pool_cache_key("debian", "main/b/bar/bar_2.0_amd64.deb")) == b"bar-deb"


def test_warm_freezes_dists_nested_package_files_under_the_cut() -> None:
    """A Docker-layout package file is stored per cut beside its indexes, fetched from the live archive only."""
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    wire_canned_suite(
        fetcher, _DOCKER_LIVE, "trixie", {"amd64": compress_packages_index(_DOCKER_PACKAGES_TEXT)}, component="stable"
    )
    deb_filename = "dists/trixie/pool/stable/amd64/docker-ce_29.6.2-1~debian.13~trixie_amd64.deb"
    fetcher.responses_by_url[f"{_DOCKER_LIVE}/{deb_filename}"] = b"docker-deb"
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request(archives=(_DOCKER_ARCHIVE,)))
    resolution = _resolve(service, "docker-ce=5:29.6.2-1~debian.13~trixie", archives=(_DOCKER_ARCHIVE,))

    result = service.warm(TIMESTAMP, resolution, (_DOCKER_ARCHIVE,), max_workers=2)

    assert result.is_complete
    assert storage.get_object(f"snap/{TIMESTAMP}/docker/{deb_filename}") == b"docker-deb"
    assert not any("snapshot.debian.org" in url for url in fetcher.fetched_urls)
    # The Worker's dists route serves exactly this key: /snap/<T>/docker/dists/<...>.
    assert storage.has_object(dists_object_key(TIMESTAMP, "docker", deb_filename[len("dists/") :]))


def test_warm_skips_already_cached_files() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    storage.put_object(pool_cache_key("debian", "main/f/foo/foo_1.0_amd64.deb"), b"already")
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = _resolve(service, "foo")

    result = service.warm(TIMESTAMP, resolution, _ARCHIVES, max_workers=2)

    assert result.is_complete
    assert result.already_cached_count == 1
    assert result.fetched_count == 0


def test_warm_reports_files_missing_on_all_upstreams_as_incomplete() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = _resolve(service, "foo")

    result = service.warm(TIMESTAMP, resolution, _ARCHIVES, max_workers=2)

    assert not result.is_complete
    assert result.missing_paths == ("debian/pool/main/f/foo/foo_1.0_amd64.deb",)
    assert result.fetched_count == 0


def test_warm_with_unresolved_specs_is_incomplete() -> None:
    storage = InMemoryAptMirrorStorage()
    fetcher = MappingUpstreamFetcher()
    _canned_suite(fetcher, FOO_PACKAGES_TEXT)
    fetcher.responses_by_url["https://deb.debian.org/debian/pool/main/f/foo/foo_1.0_amd64.deb"] = b"foo-deb"
    service = _make_service(storage=storage, fetcher=fetcher)
    service.cut(_cut_request())
    resolution = _resolve(service, "foo", "typo-name")

    result = service.warm(TIMESTAMP, resolution, _ARCHIVES, max_workers=2)

    assert not result.is_complete
    assert result.unresolved_specs == ("typo-name",)
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
    resolution = _resolve(service, "foo", "bar")
    fetcher.fetched_urls.clear()

    result = service.verify(TIMESTAMP, resolution, max_workers=2)

    assert result.cached_count == 1
    assert result.missing_paths == ("debian/pool/main/b/bar/bar_2.0_amd64.deb",)
    assert not result.is_complete
    assert fetcher.fetched_urls == []
