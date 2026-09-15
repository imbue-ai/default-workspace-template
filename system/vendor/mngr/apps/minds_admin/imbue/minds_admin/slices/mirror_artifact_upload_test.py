import hashlib

import pytest

from imbue.apt_mirror.mock_apt_mirror_test import InMemoryAptMirrorStorage
from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher
from imbue.imbue_common.model_update import to_update
from imbue.minds_admin.slices.mirror_artifact_upload import MirrorArtifactChecksumMismatchError
from imbue.minds_admin.slices.mirror_artifact_upload import MirrorArtifactUpstreamMissingError
from imbue.minds_admin.slices.mirror_artifact_upload import fetch_verified_artifact
from imbue.minds_admin.slices.mirror_artifact_upload import parse_upstream_checksum
from imbue.minds_admin.slices.mirror_artifact_upload import upload_mirror_artifacts
from imbue.minds_admin.slices.mirror_artifact_upload import verify_mirror_artifacts
from imbue.minds_admin.slices.mirror_artifacts import DigestAlgorithm
from imbue.minds_admin.slices.mirror_artifacts import MirrorArtifact
from imbue.minds_admin.slices.mirror_artifacts import MirrorArtifactError

_DATA = b"pinned-binary-bytes-40613"
_UPSTREAM = "https://upstream.example.test/releases/v1/tool-linux-amd64.tar.gz"
_CHECKSUMS = "https://upstream.example.test/releases/v1/checksums.txt"


def _artifact(checksum_url: str | None, digest: str | None = None) -> MirrorArtifact:
    return MirrorArtifact(
        name="tool",
        version="1",
        subpath="tool-linux-amd64.tar.gz",
        upstream_url=_UPSTREAM,
        digest_algorithm=DigestAlgorithm.SHA256,
        digest=digest if digest is not None else hashlib.sha256(_DATA).hexdigest(),
        upstream_checksum_url=checksum_url,
    )


def _fetcher(checksum_text: str | None = None) -> MappingUpstreamFetcher:
    fetcher = MappingUpstreamFetcher()
    fetcher.responses_by_url[_UPSTREAM] = _DATA
    if checksum_text is not None:
        fetcher.responses_by_url[_CHECKSUMS] = checksum_text.encode()
    return fetcher


def test_fetch_verified_artifact_accepts_a_download_matching_both_digests() -> None:
    fetcher = _fetcher(f"{hashlib.sha256(_DATA).hexdigest()}  tool-linux-amd64.tar.gz\n")
    assert fetch_verified_artifact(fetcher, _artifact(_CHECKSUMS)) == _DATA


def test_fetch_verified_artifact_refuses_a_download_that_does_not_match_the_recorded_digest() -> None:
    with pytest.raises(MirrorArtifactChecksumMismatchError, match="recorded sha256"):
        fetch_verified_artifact(_fetcher(), _artifact(None, digest="0" * 64))


def test_fetch_verified_artifact_refuses_when_the_upstream_checksum_file_disagrees() -> None:
    # The recorded digest matches the bytes, but upstream now publishes a
    # different one: the pin is suspect, so nothing is stored.
    fetcher = _fetcher(f"{'f' * 64}  tool-linux-amd64.tar.gz\n")
    with pytest.raises(MirrorArtifactChecksumMismatchError, match="publishes"):
        fetch_verified_artifact(fetcher, _artifact(_CHECKSUMS))


def test_fetch_verified_artifact_reports_a_vanished_upstream() -> None:
    with pytest.raises(MirrorArtifactUpstreamMissingError):
        fetch_verified_artifact(MappingUpstreamFetcher(), _artifact(None))
    with pytest.raises(MirrorArtifactUpstreamMissingError, match="checksum file"):
        fetch_verified_artifact(_fetcher(), _artifact(_CHECKSUMS))


def test_upload_mirror_artifacts_stores_missing_entries_and_skips_present_ones() -> None:
    storage = InMemoryAptMirrorStorage()
    artifact = _artifact(None)
    first = upload_mirror_artifacts(storage, _fetcher(), [artifact], is_forced=False)
    assert first.uploaded_keys == ("artifacts/tool/1/tool-linux-amd64.tar.gz",)
    assert storage.get_object("artifacts/tool/1/tool-linux-amd64.tar.gz") == _DATA

    fetcher = _fetcher()
    second = upload_mirror_artifacts(storage, fetcher, [artifact], is_forced=False)
    assert second.uploaded_keys == ()
    assert second.already_present_keys == ("artifacts/tool/1/tool-linux-amd64.tar.gz",)
    assert fetcher.fetched_urls == []

    forced = upload_mirror_artifacts(storage, _fetcher(), [artifact], is_forced=True)
    assert forced.uploaded_keys == ("artifacts/tool/1/tool-linux-amd64.tar.gz",)


def test_upload_mirror_artifacts_stores_nothing_on_a_digest_mismatch() -> None:
    storage = InMemoryAptMirrorStorage()
    with pytest.raises(MirrorArtifactChecksumMismatchError):
        upload_mirror_artifacts(storage, _fetcher(), [_artifact(None, digest="0" * 64)], is_forced=False)
    assert storage.objects_by_key == {}


def test_verify_mirror_artifacts_reports_presence_without_fetching() -> None:
    storage = InMemoryAptMirrorStorage()
    present = _artifact(None)
    missing = present.model_copy_update(to_update(present.field_ref().subpath, "tool-linux-arm64.tar.gz"))
    storage.put_object(present.object_key, _DATA)
    public_fetcher = MappingUpstreamFetcher()
    public_fetcher.responses_by_url[present.mirror_url] = _DATA

    report = verify_mirror_artifacts(storage, public_fetcher, [present, missing])

    assert report.present_keys == (present.object_key,)
    assert report.missing_keys == (missing.object_key,)
    assert report.unserved_urls == ()
    assert not report.is_complete
    # Presence is a bucket check and the public probe is a HEAD: nothing is downloaded.
    assert public_fetcher.fetched_urls == []


def test_verify_mirror_artifacts_reports_a_stored_artifact_the_worker_does_not_serve() -> None:
    # The bucket and the Worker deploy separately: an artifact can be uploaded
    # while the live Worker still lacks the route that serves it.
    storage = InMemoryAptMirrorStorage()
    artifact = _artifact(None)
    storage.put_object(artifact.object_key, _DATA)

    report = verify_mirror_artifacts(storage, MappingUpstreamFetcher(), [artifact])

    assert report.present_keys == (artifact.object_key,)
    assert report.missing_keys == ()
    assert report.unserved_urls == (artifact.mirror_url,)
    assert not report.is_complete


def test_parse_upstream_checksum_reads_sums_listings_and_single_digest_files() -> None:
    sha512 = "a" * 128
    listing = f"{'b' * 128}  other.qcow2\n{sha512}  debian-13-genericcloud-amd64.qcow2\n"
    assert parse_upstream_checksum(listing, "debian-13-genericcloud-amd64.qcow2", DigestAlgorithm.SHA512) == sha512
    binary_mode_listing = f"{'c' * 64} *s5cmd_2.3.0_Linux-64bit.tar.gz\n"
    assert parse_upstream_checksum(binary_mode_listing, "s5cmd_2.3.0_Linux-64bit.tar.gz", DigestAlgorithm.SHA256) == (
        "c" * 64
    )
    assert parse_upstream_checksum(f"{'D' * 64}\n", "uv.tar.gz", DigestAlgorithm.SHA256) == "d" * 64
    assert parse_upstream_checksum(f"{'e' * 128}  runsc\n", "runsc", DigestAlgorithm.SHA512) == "e" * 128


def test_parse_upstream_checksum_raises_when_the_file_names_no_matching_digest() -> None:
    with pytest.raises(MirrorArtifactError):
        parse_upstream_checksum(f"{'a' * 64}  other-file\n", "wanted-file", DigestAlgorithm.SHA256)
    with pytest.raises(MirrorArtifactError):
        # A sha256 where a sha512 is expected is not a match.
        parse_upstream_checksum(f"{'a' * 64}  wanted-file\n", "wanted-file", DigestAlgorithm.SHA512)
