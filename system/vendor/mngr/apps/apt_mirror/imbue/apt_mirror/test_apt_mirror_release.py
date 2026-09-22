"""Live end-to-end proof of the deployed mirror against the default-workspace-template's pins.

Release-only: it depends on the deployed Worker, the R2 bucket, a cut of the
template's committed timestamp, Docker Hub, and Docker locally, none of which
per-PR CI should be coupled to. Every test reads the template's pins (the
Dockerfile's digest-pinned base image and ``.mngr/apt-snapshot-timestamp``)
from the template ref named by ``DEFAULT_WORKSPACE_TEMPLATE_REF`` (default
``main``); a release cut runs it against the template release branch.
"""

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest
from loguru import logger

from imbue.apt_mirror.data_types import APT_MIRROR_PUBLIC_BASE_URL
from imbue.apt_mirror.data_types import ArchiveSource
from imbue.apt_mirror.data_types import DEBIAN_ARCHIVE
from imbue.apt_mirror.data_types import DEBIAN_SECURITY_ARCHIVE
from imbue.apt_mirror.data_types import DEFAULT_ARCHITECTURES
from imbue.apt_mirror.data_types import PackagesIndexEntry
from imbue.apt_mirror.fetcher import open_http_upstream_fetcher
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface
from imbue.apt_mirror.parsing import PACKAGES_INDEX_NAMES
from imbue.apt_mirror.parsing import find_packages_newer_than_or_absent_from_index
from imbue.apt_mirror.parsing import parse_dpkg_status_installed_packages
from imbue.apt_mirror.parsing import parse_packages_index_entries
from imbue.apt_mirror.parsing import snapshot_archive_url
from imbue.apt_mirror.template_base_image import PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP
from imbue.apt_mirror.template_base_image import read_default_workspace_template_pin

_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0
_DOCKER_TIMEOUT_SECONDS: Final[float] = 300.0
# An apt update + install against the live mirror, inside the 600s test timeout.
_APT_INSTALL_TIMEOUT_SECONDS: Final[float] = 540.0
# The frozen Debian archives the template's apt sources point at (write_apt_sources.sh).
_TEMPLATE_APT_ARCHIVES: Final[tuple[ArchiveSource, ...]] = (DEBIAN_ARCHIVE, DEBIAN_SECURITY_ARCHIVE)
# Installed by the template's setup_system.sh; each depends on the exact
# version of a package the base itself ships (libc6, perl-base, libsqlite3-0),
# so a base newer than the snapshot makes them uninstallable.
_TOOLCHAIN_PACKAGES_TIED_TO_THE_BASE: Final[tuple[str, ...]] = ("libc6-dev", "perl", "sqlite3")
# Every (snapshot timestamp, pinned base) pair the seed of a floating-FROM template builds against.
_PINNED_BASE_IMAGES_BY_TIMESTAMP: Final[tuple[tuple[str, str], ...]] = tuple(
    sorted(PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP.items())
)


def _pinned_sources_script(timestamp: str, packages: tuple[str, ...]) -> str:
    """A container script mirroring dwt's write_apt_sources.sh: pin sources, update, install ``packages``."""
    debian_uri = snapshot_archive_url(APT_MIRROR_PUBLIC_BASE_URL, timestamp, DEBIAN_ARCHIVE.name)
    security_uri = snapshot_archive_url(APT_MIRROR_PUBLIC_BASE_URL, timestamp, DEBIAN_SECURITY_ARCHIVE.name)
    return "\n".join(
        [
            "set -euo pipefail",
            "rm -f /etc/apt/sources.list.d/debian.sources",
            ": > /etc/apt/sources.list",
            "cat > /etc/apt/sources.list.d/pinned.sources <<SOURCES",
            "Types: deb",
            f"URIs: {debian_uri}",
            "Suites: trixie trixie-updates",
            "Components: main",
            "Check-Valid-Until: no",
            "",
            "Types: deb",
            f"URIs: {security_uri}",
            "Suites: trixie-security",
            "Components: main",
            "Check-Valid-Until: no",
            "SOURCES",
            "apt-get update",
            f"apt-get install -y --no-install-recommends {' '.join(packages)}",
            "dpkg-query -W -f='${Package}\\t${Version}\\n' " + " ".join(packages),
        ]
    )


def _install_from_pinned_sources(image_ref: str, timestamp: str, packages: tuple[str, ...]) -> str:
    """Install ``packages`` from the ``timestamp`` snapshot inside a fresh ``image_ref`` container; returns its stdout."""
    result = subprocess.run(
        ["docker", "run", "--rm", image_ref, "bash", "-c", _pinned_sources_script(timestamp, packages)],
        capture_output=True,
        text=True,
        timeout=_APT_INSTALL_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"installing {packages} from the {timestamp} snapshot on {image_ref} failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.timeout(600)
def test_live_mirror_serves_apt_update_and_install_in_the_template_base_image() -> None:
    pin = read_default_workspace_template_pin(os.environ)

    stdout = _install_from_pinned_sources(pin.base_image_ref, pin.apt_snapshot_timestamp, ("jq",))

    assert "jq\t" in stdout


@contextmanager
def _created_container(image_ref: str, platform: str) -> Iterator[str]:
    """A container created (never started) from ``image_ref`` for ``platform``, removed on exit.

    Creating rather than running lets the arm64 variant be inspected on an
    amd64 host with no emulation: ``docker cp`` reads its filesystem.
    """
    created = subprocess.run(
        ["docker", "create", "--platform", platform, image_ref],
        capture_output=True,
        text=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    assert created.returncode == 0, f"docker create {image_ref} for {platform} failed:\n{created.stderr}"
    container_id = created.stdout.strip()
    try:
        yield container_id
    finally:
        removed = subprocess.run(
            ["docker", "rm", "-f", container_id], capture_output=True, text=True, timeout=_DOCKER_TIMEOUT_SECONDS
        )
        if removed.returncode != 0:
            logger.warning(
                "Failed to remove container {} created from {}: {}", container_id, image_ref, removed.stderr.strip()
            )


def _dpkg_status_of_image(image_ref: str, platform: str, destination_dir: Path) -> str:
    """The image's ``/var/lib/dpkg/status``, copied into ``destination_dir`` without starting a container."""
    with _created_container(image_ref, platform) as container_id:
        status_path = destination_dir / "status"
        copied = subprocess.run(
            ["docker", "cp", f"{container_id}:/var/lib/dpkg/status", str(status_path)],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT_SECONDS,
        )
        assert copied.returncode == 0, f"docker cp of dpkg status from {image_ref} failed:\n{copied.stderr}"
        return status_path.read_text(encoding="utf-8", errors="replace")


def _fetch_packages_index_entries(
    fetcher: UpstreamFetcherInterface, archive_root: str, archive: ArchiveSource, suite: str, arch: str
) -> list[PackagesIndexEntry]:
    """The stanzas of the first Packages index spelling the mirror serves for one suite's required component and ``arch``."""
    for index_name in PACKAGES_INDEX_NAMES:
        index_path = f"dists/{suite}/{archive.required_component}/binary-{arch}/{index_name}"
        index_data = fetcher.fetch(f"{archive_root}/{index_path}")
        if index_data is not None:
            return parse_packages_index_entries(index_data, index_path)
    pytest.fail(f"the mirror serves no Packages index for {archive.name} {suite} {arch}")


def _mirror_index_entries(fetcher: UpstreamFetcherInterface, timestamp: str, arch: str) -> list[PackagesIndexEntry]:
    """Every stanza the live mirror's frozen main-component indexes list for ``arch`` at ``timestamp``."""
    entries: list[PackagesIndexEntry] = []
    for archive in _TEMPLATE_APT_ARCHIVES:
        archive_root = snapshot_archive_url(APT_MIRROR_PUBLIC_BASE_URL, timestamp, archive.name)
        for suite in archive.suites:
            entries.extend(_fetch_packages_index_entries(fetcher, archive_root, archive, suite, arch))
    return entries


def _assert_image_packages_are_covered_by_snapshot(
    image_ref: str, timestamp: str, arch: str, tmp_path: Path, remedy: str
) -> None:
    """Fail naming every package ``image_ref`` ships that the frozen index at ``timestamp`` cannot keep."""
    installed = parse_dpkg_status_installed_packages(_dpkg_status_of_image(image_ref, f"linux/{arch}", tmp_path))
    assert installed, f"{image_ref} reports no installed packages for {arch}"
    with open_http_upstream_fetcher(_HTTP_TIMEOUT_SECONDS) as fetcher:
        index_entries = _mirror_index_entries(fetcher, timestamp, arch)
    mismatches = [
        mismatch.text for mismatch in find_packages_newer_than_or_absent_from_index(installed, index_entries)
    ]
    assert not mismatches, (
        f"{image_ref} ({arch}) ships packages newer than, or absent from, the {timestamp} snapshot's index, so apt "
        f"cannot install the template's toolchain on it without downgrading; {remedy}: {mismatches}"
    )


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.timeout(600)
@pytest.mark.parametrize("arch", DEFAULT_ARCHITECTURES)
def test_template_base_image_packages_are_installable_from_its_pinned_snapshot(arch: str, tmp_path: Path) -> None:
    """No package the pinned base ships may be newer than the snapshot the template pins.

    apt never downgrades, so a base whose libc6 (say) is newer than the frozen
    index makes ``apt-get install libc6-dev`` unsatisfiable and every fresh
    image build fails (imbue-ai/mngr-internal#1138); an older base is fine,
    apt upgrades it. The two pins are bumped together; this proves the pair
    still agrees, per architecture, and that the pinned digest is still
    pullable from Docker Hub.
    """
    pin = read_default_workspace_template_pin(os.environ)
    _assert_image_packages_are_covered_by_snapshot(
        pin.base_image_ref,
        pin.apt_snapshot_timestamp,
        arch,
        tmp_path,
        remedy="bump the Dockerfile digest and .mngr/apt-snapshot-timestamp together",
    )


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.timeout(600)
@pytest.mark.parametrize("arch", DEFAULT_ARCHITECTURES)
@pytest.mark.parametrize(("timestamp", "pinned_image_ref"), _PINNED_BASE_IMAGES_BY_TIMESTAMP)
def test_floating_base_override_ships_packages_its_snapshot_covers(
    timestamp: str, pinned_image_ref: str, arch: str, tmp_path: Path
) -> None:
    """Each recorded (snapshot, pinned base) pair must still agree, and the digest must still be pullable.

    The pool bake seeds a tag whose Dockerfile floats its base against the
    base this table records for the tag's snapshot (imbue-ai/mngr-internal#1143);
    an entry whose manifest Docker Hub has dropped, or whose packages the
    snapshot cannot keep, would fail every such seed.
    """
    _assert_image_packages_are_covered_by_snapshot(
        pinned_image_ref,
        timestamp,
        arch,
        tmp_path,
        remedy="fix the PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP entry",
    )


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.timeout(600)
@pytest.mark.parametrize(("timestamp", "pinned_image_ref"), _PINNED_BASE_IMAGES_BY_TIMESTAMP)
def test_floating_base_override_installs_the_toolchain_tied_to_the_base(timestamp: str, pinned_image_ref: str) -> None:
    """The recorded base installs libc6-dev, perl and sqlite3 from its snapshot, the installs a too-new base breaks."""
    stdout = _install_from_pinned_sources(pinned_image_ref, timestamp, _TOOLCHAIN_PACKAGES_TIED_TO_THE_BASE)

    for package in _TOOLCHAIN_PACKAGES_TIED_TO_THE_BASE:
        assert f"{package}\t" in stdout, f"{package} is not installed:\n{stdout}"
