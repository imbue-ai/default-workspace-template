"""Pinned, hash-verified install of the ``onetun`` userspace WireGuard binary.

The box-management dial resolver (``slices/box_access.py``) prefers a per-box
userspace WireGuard tunnel spawned from the ``onetun`` binary. This module
pins the exact release that transport is verified against and installs it to
the well-known location the resolver's discovery checks
(``~/.mindsadmin/bin/onetun``), so ``minds-admin wireguard
install-onetun`` is all an operator (or a CI workflow) needs to run.

onetun is remote-root tooling (it carries the operator's WireGuard identity to
the fleet's management SSH), so the download is never trusted as-is: each
platform's release asset is verified against the sha256 the artifact manifest
(``slices/mirror_artifacts.py``) records, computed from the official GitHub
release binaries at pin time. The binary itself is downloaded from imbue's
artifact mirror, never from GitHub, so a pruned upstream release cannot strand
an operator.
"""

import hashlib
import platform
import time
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.minds_admin.slices.mirror_artifacts import MirrorArtifact
from imbue.minds_admin.slices.mirror_artifacts import ONETUN_ARTIFACT_BY_PLATFORM
from imbue.minds_admin.slices.mirror_artifacts import ONETUN_VERSION
from imbue.minds_admin.slices.operator_identity import operator_identity_root

# Two-threshold download timeouts: the hard bound catches a wedged transfer
# outright; the warning threshold surfaces a slowing mirror download before it
# becomes an outage.
_DOWNLOAD_HARD_TIMEOUT_SECONDS: Final[float] = 120.0
_DOWNLOAD_WARNING_THRESHOLD_SECONDS: Final[float] = 30.0

_VERSION_CHECK_TIMEOUT_SECONDS: Final[float] = 15.0


class OnetunInstallError(MindError):
    """Raised when the pinned onetun binary cannot be installed or verified."""


def well_known_onetun_path() -> Path:
    """The install destination the dial resolver's discovery checks after MNGR_ONETUN_PATH and PATH."""
    return operator_identity_root() / "bin" / "onetun"


class OnetunInstallResult(FrozenModel):
    """What ``install_pinned_onetun`` produced (or found already in place)."""

    binary_path: Path = Field(description="Where the pinned onetun binary now lives")
    version: str = Field(description="The installed onetun version")
    was_already_current: bool = Field(description="Whether the pinned version was already installed (no download)")


@pure
def select_onetun_release_asset(system: str, machine: str) -> MirrorArtifact:
    """The pinned release's mirrored asset (name, digest, mirror URL) for this platform.

    Raises OnetunInstallError for platforms without a prebuilt asset in the
    pinned release (Windows, macOS Intel).
    """
    artifact = ONETUN_ARTIFACT_BY_PLATFORM.get((system, machine))
    if artifact is None:
        supported = sorted(f"{s}/{m}" for s, m in ONETUN_ARTIFACT_BY_PLATFORM)
        raise OnetunInstallError(
            f"no prebuilt onetun {ONETUN_VERSION} binary for {system}/{machine} (supported: {', '.join(supported)}); "
            "install it another way (e.g. `cargo install onetun --version "
            f"{ONETUN_VERSION}`) and point MNGR_ONETUN_PATH at it"
        )
    return artifact


@pure
def parse_onetun_version_output(stdout: str) -> str | None:
    """The version from ``onetun --version`` output (``onetun 0.3.10``), or None when unparsable."""
    parts = stdout.strip().split()
    if len(parts) == 2 and parts[0] == "onetun":
        return parts[1]
    return None


def installed_onetun_version_or_none(binary_path: Path, parent_cg: ConcurrencyGroup) -> str | None:
    """The version an existing binary reports, or None when it is absent or does not answer.

    OSError covers a binary that cannot be spawned at all (wrong architecture,
    lost exec bit); reporting it as None lets the installer refresh it.
    """
    if not binary_path.is_file():
        return None
    try:
        finished = parent_cg.run_process_to_completion(
            command=[str(binary_path), "--version"],
            timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
            is_checked_after=True,
            name="onetun-version-check",
        )
    except (OSError, ProcessError) as exc:
        logger.warning("Failed to read the version of the onetun binary at {}: {}", binary_path, exc)
        return None
    return parse_onetun_version_output(finished.stdout)


def _download_release_asset(url: str) -> bytes:
    started_at = time.monotonic()
    try:
        response = httpx.get(url, follow_redirects=True, timeout=_DOWNLOAD_HARD_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OnetunInstallError(
            f"failed to download the pinned onetun release from the artifact mirror at {url}: {exc} "
            "(run `minds-admin artifacts upload --name onetun` if it was never uploaded)"
        ) from exc
    elapsed = time.monotonic() - started_at
    if elapsed > _DOWNLOAD_WARNING_THRESHOLD_SECONDS:
        logger.warning("Downloaded the onetun release slowly ({:.1f}s from {})", elapsed, url)
    return response.content


def write_verified_onetun_binary(data: bytes, expected_sha256: str, destination: Path) -> None:
    """Verify the downloaded bytes against the pinned hash and install them executably (atomic rename).

    Raises OnetunInstallError on a hash mismatch -- nothing is written in that
    case, so a tampered or truncated download can never land on disk.
    """
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise OnetunInstallError(
            f"onetun download hash mismatch: expected {expected_sha256}, got {actual_sha256}; "
            "refusing to install the binary"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_path = destination.with_name(destination.name + ".mngr-tmp")
    staging_path.write_bytes(data)
    staging_path.chmod(0o755)
    staging_path.replace(destination)


def install_pinned_onetun(parent_cg: ConcurrencyGroup) -> OnetunInstallResult:
    """Install the pinned onetun release to the well-known path (no-op when already current).

    Non-interactive by design so CI workflows can run the same step. An
    existing binary at the destination is version-checked and refreshed on any
    mismatch (including one that fails to answer ``--version``).
    """
    destination = well_known_onetun_path()
    installed_version = installed_onetun_version_or_none(destination, parent_cg)
    if installed_version == ONETUN_VERSION:
        return OnetunInstallResult(binary_path=destination, version=ONETUN_VERSION, was_already_current=True)
    artifact = select_onetun_release_asset(platform.system(), platform.machine())
    logger.info("Downloading onetun {} ({}) from {}", ONETUN_VERSION, artifact.subpath, artifact.mirror_url)
    data = _download_release_asset(artifact.mirror_url)
    write_verified_onetun_binary(data, artifact.digest, destination)
    verified_version = installed_onetun_version_or_none(destination, parent_cg)
    if verified_version != ONETUN_VERSION:
        raise OnetunInstallError(
            f"the freshly installed onetun binary at {destination} reports version "
            f"{verified_version!r} instead of the pinned {ONETUN_VERSION}"
        )
    return OnetunInstallResult(binary_path=destination, version=ONETUN_VERSION, was_already_current=False)
