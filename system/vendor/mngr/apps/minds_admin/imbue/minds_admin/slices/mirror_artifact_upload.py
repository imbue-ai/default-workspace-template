"""Populating the artifact mirror: download each pinned upstream file, verify it, store it in the bucket."""

import hashlib
import re
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.apt_mirror.interfaces import AptMirrorStorageInterface
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.minds_admin.slices.mirror_artifacts import DigestAlgorithm
from imbue.minds_admin.slices.mirror_artifacts import MirrorArtifact
from imbue.minds_admin.slices.mirror_artifacts import MirrorArtifactError


class MirrorArtifactUpstreamMissingError(MirrorArtifactError):
    """Raised when an artifact (or its checksum file) is no longer served upstream."""


class MirrorArtifactChecksumMismatchError(MirrorArtifactError):
    """Raised when a downloaded artifact does not match its recorded or upstream-published digest."""


class MirrorArtifactUploadReport(FrozenModel):
    """Outcome of an upload pass over a set of manifest entries."""

    uploaded_keys: tuple[str, ...] = Field(description="Artifacts fetched, verified, and stored by this pass")
    already_present_keys: tuple[str, ...] = Field(description="Artifacts the bucket already held (skipped)")


class MirrorArtifactVerifyReport(FrozenModel):
    """Outcome of a read-only check that the mirror holds and serves a set of manifest entries."""

    present_keys: tuple[str, ...] = Field(description="Artifacts the bucket holds")
    missing_keys: tuple[str, ...] = Field(description="Artifacts the bucket lacks (run the upload)")
    unserved_urls: tuple[str, ...] = Field(
        description=(
            "Public mirror URLs of artifacts the bucket holds but the live Worker does not serve "
            "(redeploy the Worker: its route table is deployed separately from the bucket's content)"
        )
    )

    @property
    def is_complete(self) -> bool:
        return not self.missing_keys and not self.unserved_urls


@pure
def compute_digest(data: bytes, algorithm: DigestAlgorithm) -> str:
    match algorithm:
        case DigestAlgorithm.SHA256:
            return hashlib.sha256(data).hexdigest()
        case DigestAlgorithm.SHA512:
            return hashlib.sha512(data).hexdigest()
        case _ as unreachable:
            assert_never(unreachable)


_HEX_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]+$")
_DIGEST_HEX_LENGTH_BY_ALGORITHM: Final[Mapping[DigestAlgorithm, int]] = {
    DigestAlgorithm.SHA256: 64,
    DigestAlgorithm.SHA512: 128,
}


@pure
def parse_upstream_checksum(checksum_text: str, file_name: str, algorithm: DigestAlgorithm) -> str:
    """The digest an upstream checksum file records for ``file_name``.

    Accepts the two shapes upstreams publish: a sums listing (``<hex>  <name>``
    per line, with or without the binary-mode ``*`` prefix) and a file holding
    a single digest, optionally followed by the file name. Raises
    MirrorArtifactError when the file names no digest of the right length.
    """
    expected_length = _DIGEST_HEX_LENGTH_BY_ALGORITHM[algorithm]
    for line in checksum_text.splitlines():
        parts = line.split()
        if not parts or not _HEX_DIGEST_RE.match(parts[0]) or len(parts[0]) != expected_length:
            continue
        if len(parts) == 1 or parts[1].lstrip("*") == file_name:
            return parts[0].lower()
    raise MirrorArtifactError(f"upstream checksum file names no {algorithm.value.lower()} digest for {file_name!r}")


def fetch_verified_artifact(fetcher: UpstreamFetcherInterface, artifact: MirrorArtifact) -> bytes:
    """Download one artifact from upstream and verify it against the recorded digest (and the upstream checksum file, when one exists).

    Raises MirrorArtifactUpstreamMissingError when upstream no longer serves
    the file, and MirrorArtifactChecksumMismatchError on any digest
    disagreement -- nothing is stored in that case.
    """
    data = fetcher.fetch(artifact.upstream_url)
    if data is None:
        raise MirrorArtifactUpstreamMissingError(f"{artifact.upstream_url} is no longer served upstream")
    actual_digest = compute_digest(data, artifact.digest_algorithm)
    if actual_digest != artifact.digest:
        raise MirrorArtifactChecksumMismatchError(
            f"{artifact.upstream_url}: recorded {artifact.digest_algorithm.value.lower()} {artifact.digest}, "
            f"downloaded {actual_digest}"
        )
    if artifact.upstream_checksum_url is not None:
        checksum_data = fetcher.fetch(artifact.upstream_checksum_url)
        if checksum_data is None:
            raise MirrorArtifactUpstreamMissingError(
                f"{artifact.upstream_checksum_url} (the upstream checksum file) is no longer served"
            )
        published_digest = parse_upstream_checksum(
            checksum_data.decode("utf-8", errors="replace"), artifact.file_name, artifact.digest_algorithm
        )
        if published_digest != artifact.digest:
            raise MirrorArtifactChecksumMismatchError(
                f"{artifact.upstream_checksum_url} publishes {published_digest} for {artifact.file_name}, "
                f"but the manifest records {artifact.digest}"
            )
    return data


def upload_mirror_artifacts(
    storage: AptMirrorStorageInterface,
    fetcher: UpstreamFetcherInterface,
    artifacts: Sequence[MirrorArtifact],
    is_forced: bool,
) -> MirrorArtifactUploadReport:
    """Store every given artifact that the bucket lacks (or all of them when forced), verifying each first."""
    uploaded: list[str] = []
    already_present: list[str] = []
    for artifact in artifacts:
        key = artifact.object_key
        if not is_forced and storage.has_object(key):
            already_present.append(key)
            continue
        with log_span("Mirroring {} from {}", key, artifact.upstream_url):
            data = fetch_verified_artifact(fetcher, artifact)
            storage.put_object(key, data)
        logger.info("Uploaded {} ({} bytes)", key, len(data))
        uploaded.append(key)
    return MirrorArtifactUploadReport(uploaded_keys=tuple(uploaded), already_present_keys=tuple(already_present))


def verify_mirror_artifacts(
    storage: AptMirrorStorageInterface,
    # Probes each stored artifact's public mirror URL: consumers download through
    # the Worker, whose route table is deployed independently of the bucket.
    public_fetcher: UpstreamFetcherInterface,
    artifacts: Sequence[MirrorArtifact],
) -> MirrorArtifactVerifyReport:
    """Read-only check that the bucket holds every given artifact and the live Worker serves it."""
    present: list[str] = []
    missing: list[str] = []
    unserved: list[str] = []
    for artifact in artifacts:
        if not storage.has_object(artifact.object_key):
            missing.append(artifact.object_key)
            continue
        present.append(artifact.object_key)
        if not public_fetcher.is_served(artifact.mirror_url):
            unserved.append(artifact.mirror_url)
    return MirrorArtifactVerifyReport(
        present_keys=tuple(present), missing_keys=tuple(missing), unserved_urls=tuple(unserved)
    )
