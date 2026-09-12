"""The manifest of pinned upstream artifacts the fleet fetches from imbue's mirror.

Every gen-2 box prep, guest-image bake, collector install, and operator onetun
install downloads its pinned binaries from ``https://apt.imbuepackages.com``
(the apt mirror's ``/artifacts/<name>/<version>/<subpath>`` route) instead of
from upstream, so an upstream pruning an old release can never break a prep
or a repave. This module is the single source of truth for that set: each
entry names the upstream file, the mirror location, and the digest the
operator upload verifies before storing and the consumer re-verifies after
downloading. ``minds-admin artifacts upload`` is what populates the mirror.
"""

import posixpath
from collections.abc import Mapping
from enum import auto
from typing import Final

from pydantic import Field

from imbue.apt_mirror.data_types import APT_MIRROR_PUBLIC_BASE_URL
from imbue.apt_mirror.parsing import artifact_object_key
from imbue.apt_mirror.parsing import artifact_url
from imbue.apt_mirror.parsing import artifact_version_url
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.errors import MindError
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE
from imbue.mngr_vps.host_setup import PINNED_GVISOR_UPSTREAM_RELEASE_URL
from imbue.observability.collector_install import OTELCOL_CONTRIB_ARTIFACT_NAME
from imbue.observability.collector_install import OTELCOL_CONTRIB_VERSION
from imbue.observability.collector_install import OTELCOL_DEB_SHA256_BY_GOARCH
from imbue.observability.collector_install import otelcol_contrib_deb_filename
from imbue.observability.collector_install import otelcol_contrib_deb_upstream_url


class MirrorArtifactError(MindError):
    """Raised when an artifact manifest entry or an upstream checksum file cannot be used."""


class DigestAlgorithm(UpperCaseStrEnum):
    """The hash an artifact is pinned by (whichever its upstream publishes)."""

    SHA256 = auto()
    SHA512 = auto()


class MirrorArtifact(FrozenModel):
    """One pinned upstream file and where the mirror serves it."""

    name: str = Field(description="Artifact name (the first URL segment under /artifacts/)")
    version: str = Field(description="The pinned upstream release (the second URL segment)")
    subpath: str = Field(description="The file's path under the version directory, mirroring the upstream layout")
    upstream_url: str = Field(description="Where the operator upload fetches the file from")
    digest_algorithm: DigestAlgorithm = Field(description="Which hash pins the file")
    digest: str = Field(description="The hex digest recorded at pin time; uploads and installs both verify it")
    upstream_checksum_url: str | None = Field(
        description="An upstream-published checksum file (a sha sums listing or a single digest) the upload cross-checks, if any"
    )

    @property
    def file_name(self) -> str:
        return posixpath.basename(self.subpath)

    @property
    def object_key(self) -> str:
        return artifact_object_key(self.name, self.version, self.subpath)

    @property
    def mirror_url(self) -> str:
        return artifact_url(APT_MIRROR_PUBLIC_BASE_URL, self.name, self.version, self.subpath)


# The Debian 13 "trixie" genericcloud release both the gen-2 slice guests and
# the desktop Lima VMs (default-workspace-template's [providers.lima] pins)
# boot. Bump the release here and the dwt pins together -- the minds release
# checklist (apps/minds/docs/deploy/ops/app-release.md) carries the reminder.
# Upstream: https://cloud.debian.org/images/cloud/trixie/<release>/ (pruned
# of old releases, which is why the images are mirrored).
DEBIAN_CLOUD_IMAGE_RELEASE: Final[str] = "20260722-2547"
_DEBIAN_CLOUD_IMAGE_UPSTREAM_DIR: Final[str] = (
    f"https://cloud.debian.org/images/cloud/trixie/{DEBIAN_CLOUD_IMAGE_RELEASE}"
)
_DEBIAN_CLOUD_IMAGE_SHA512SUMS_URL: Final[str] = f"{_DEBIAN_CLOUD_IMAGE_UPSTREAM_DIR}/SHA512SUMS"


def _debian_cloud_image(arch: str, sha512: str) -> MirrorArtifact:
    file_name = f"debian-13-genericcloud-{arch}-{DEBIAN_CLOUD_IMAGE_RELEASE}.qcow2"
    return MirrorArtifact(
        name="debian-cloud-image",
        version=DEBIAN_CLOUD_IMAGE_RELEASE,
        subpath=file_name,
        upstream_url=f"{_DEBIAN_CLOUD_IMAGE_UPSTREAM_DIR}/{file_name}",
        digest_algorithm=DigestAlgorithm.SHA512,
        digest=sha512,
        upstream_checksum_url=_DEBIAN_CLOUD_IMAGE_SHA512SUMS_URL,
    )


GEN2_SLICE_GUEST_IMAGE: Final[MirrorArtifact] = _debian_cloud_image(
    "amd64",
    "735d1b2d0ef265a0c2323fdaa7d46e7bd7a1b984f73e8a785e638034bf07876e26374a9d809d713501270c071b3464d2ada0c5589f07742b95ed853cc6d48f45",
)
DESKTOP_LIMA_GUEST_IMAGE_ARM64: Final[MirrorArtifact] = _debian_cloud_image(
    "arm64",
    "fce97f7642f23709bc6a8a146624a5e30d3f61603571a2d70f974ed5bd2d5d06ba0605a2fc954a8f9b02625ab6a1af182bbaa721f0c21380b8991a1f7f1a8f87",
)

# gVisor: the pinned release's amd64 binaries plus the checksum files the
# shared install script (libs/mngr_vps/.../host_setup.py) verifies them with,
# in the upstream per-arch layout so only the base URL changes. Upstream:
# https://storage.googleapis.com/gvisor/releases/release/<release>/x86_64/.
GVISOR_ARTIFACT_NAME: Final[str] = "gvisor"
GVISOR_MIRROR_RELEASE_URL: Final[str] = artifact_version_url(
    APT_MIRROR_PUBLIC_BASE_URL, GVISOR_ARTIFACT_NAME, PINNED_GVISOR_RELEASE
)


def _gvisor_file(file_name: str, algorithm: DigestAlgorithm, digest: str, checksum_url: str | None) -> MirrorArtifact:
    return MirrorArtifact(
        name=GVISOR_ARTIFACT_NAME,
        version=PINNED_GVISOR_RELEASE,
        subpath=f"x86_64/{file_name}",
        upstream_url=f"{PINNED_GVISOR_UPSTREAM_RELEASE_URL}/x86_64/{file_name}",
        digest_algorithm=algorithm,
        digest=digest,
        upstream_checksum_url=checksum_url,
    )


_GVISOR_ARTIFACTS: Final[tuple[MirrorArtifact, ...]] = (
    _gvisor_file(
        "runsc",
        DigestAlgorithm.SHA512,
        "ea985c4a505a37c50726b35fa5d48fdb8058dce36833d7710d49be035e08623b425869f91deb9133c1c5b35a33a77aed5ddcbc6926801a2dc49ddb0805aed7dc",
        f"{PINNED_GVISOR_UPSTREAM_RELEASE_URL}/x86_64/runsc.sha512",
    ),
    _gvisor_file(
        "runsc.sha512",
        DigestAlgorithm.SHA256,
        "3e4f95ee80e86ca5e6edb3cf1424e8e8f4abe461071f304daee478d364013719",
        None,
    ),
    _gvisor_file(
        "containerd-shim-runsc-v1",
        DigestAlgorithm.SHA512,
        "03db6ad270bf0500bca64a4a664faa09b996b06992846d37c8224c2c21bcfe9234a5f3b3a0edd7b68bd39d28f69b303bc23f8179dc511923278296e90ec9b38d",
        f"{PINNED_GVISOR_UPSTREAM_RELEASE_URL}/x86_64/containerd-shim-runsc-v1.sha512",
    ),
    _gvisor_file(
        "containerd-shim-runsc-v1.sha512",
        DigestAlgorithm.SHA256,
        "45b84e1c093b5112e85f09edbad78eea8a65cd42ef887190fb81207ec7798581",
        None,
    ),
)

# Transfer tooling for workspace stop/start (installed on every box). age
# publishes only sigsum proofs, so its digest was recorded from the release
# asset at pin time; s5cmd publishes a checksums listing.
# Upstream: https://github.com/FiloSottile/age/releases, https://github.com/peak/s5cmd/releases.
AGE_VERSION: Final[str] = "1.2.1"
AGE_TARBALL: Final[MirrorArtifact] = MirrorArtifact(
    name="age",
    version=AGE_VERSION,
    subpath=f"age-v{AGE_VERSION}-linux-amd64.tar.gz",
    upstream_url=f"https://github.com/FiloSottile/age/releases/download/v{AGE_VERSION}/age-v{AGE_VERSION}-linux-amd64.tar.gz",
    digest_algorithm=DigestAlgorithm.SHA256,
    digest="7df45a6cc87d4da11cc03a539a7470c15b1041ab2b396af088fe9990f7c79d50",
    upstream_checksum_url=None,
)
S5CMD_VERSION: Final[str] = "2.3.0"
S5CMD_TARBALL: Final[MirrorArtifact] = MirrorArtifact(
    name="s5cmd",
    version=S5CMD_VERSION,
    subpath=f"s5cmd_{S5CMD_VERSION}_Linux-64bit.tar.gz",
    upstream_url=f"https://github.com/peak/s5cmd/releases/download/v{S5CMD_VERSION}/s5cmd_{S5CMD_VERSION}_Linux-64bit.tar.gz",
    digest_algorithm=DigestAlgorithm.SHA256,
    digest="de0fdbfa3aceae55e069ba81a0fc17b2026567637603734a387b2fca06c299b4",
    upstream_checksum_url=f"https://github.com/peak/s5cmd/releases/download/v{S5CMD_VERSION}/s5cmd_checksums.txt",
)

# uv for the box's slice service user (runs the vendored mngr that drives the
# bake): the release tarball rather than the astral.sh install stream, pinned
# to the version default-workspace-template's setup_system.sh installs.
# Upstream: https://github.com/astral-sh/uv/releases.
UV_VERSION: Final[str] = "0.11.7"
UV_TARBALL: Final[MirrorArtifact] = MirrorArtifact(
    name="uv",
    version=UV_VERSION,
    subpath="uv-x86_64-unknown-linux-gnu.tar.gz",
    upstream_url=f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz",
    digest_algorithm=DigestAlgorithm.SHA256,
    digest="6681d691eb7f9c00ac6a3af54252f7ab29ae72f0c8f95bdc7f9d1401c23ea868",
    upstream_checksum_url=f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz.sha256",
)

# onetun, the operator-side userspace WireGuard transport (slices/onetun_install.py).
# Upstream publishes no checksums; the digests were recorded from the official
# release binaries at pin time. A version bump must re-check ``onetun --help``
# against the dial resolver's argv renderer and recompute every digest.
# Upstream: https://github.com/aramperes/onetun/releases.
ONETUN_VERSION: Final[str] = "0.3.10"


def _onetun_binary(asset_name: str, sha256: str) -> MirrorArtifact:
    return MirrorArtifact(
        name="onetun",
        version=ONETUN_VERSION,
        subpath=asset_name,
        upstream_url=f"https://github.com/aramperes/onetun/releases/download/v{ONETUN_VERSION}/{asset_name}",
        digest_algorithm=DigestAlgorithm.SHA256,
        digest=sha256,
        upstream_checksum_url=None,
    )


# Keyed by ``(platform.system(), platform.machine())``. macOS Intel has no
# prebuilt asset in this release (upstream dropped it), so it is deliberately
# absent -- the installer refuses with a clear error there.
ONETUN_ARTIFACT_BY_PLATFORM: Final[Mapping[tuple[str, str], MirrorArtifact]] = {
    ("Linux", "x86_64"): _onetun_binary(
        "onetun-linux-amd64", "1c2918da3ee3b3d0f522aa82d29c194741593737df85a73b0c6ce15502c00d15"
    ),
    ("Linux", "aarch64"): _onetun_binary(
        "onetun-linux-aarch64", "1cb94c47da4bffe622d5efe4ffb5ba3bde8e4f89c2362c3b99b69df483fd728a"
    ),
    ("Darwin", "arm64"): _onetun_binary(
        "onetun-macos-aarch64", "944911ac292590f5be763aa5f99ba47256d4ff33746e4f5831bc79ece343a4af"
    ),
}


def _otelcol_deb(goarch: str) -> MirrorArtifact:
    return MirrorArtifact(
        name=OTELCOL_CONTRIB_ARTIFACT_NAME,
        version=OTELCOL_CONTRIB_VERSION,
        subpath=otelcol_contrib_deb_filename(goarch),
        upstream_url=otelcol_contrib_deb_upstream_url(goarch),
        digest_algorithm=DigestAlgorithm.SHA256,
        digest=OTELCOL_DEB_SHA256_BY_GOARCH[goarch],
        upstream_checksum_url=None,
    )


# Every artifact the mirror must hold. Order is the upload order.
MIRROR_ARTIFACTS: Final[tuple[MirrorArtifact, ...]] = (
    GEN2_SLICE_GUEST_IMAGE,
    DESKTOP_LIMA_GUEST_IMAGE_ARM64,
    *_GVISOR_ARTIFACTS,
    AGE_TARBALL,
    S5CMD_TARBALL,
    UV_TARBALL,
    *ONETUN_ARTIFACT_BY_PLATFORM.values(),
    _otelcol_deb("amd64"),
    _otelcol_deb("arm64"),
)
