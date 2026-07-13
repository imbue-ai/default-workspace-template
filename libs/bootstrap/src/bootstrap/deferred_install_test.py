"""Unit tests for the secret-scanner wrappers in scripts/deferred_install.sh.

The wrappers add deferred-install marker semantics around the shared pinned
installer (scripts/install_secret_scanners.sh, which has its own tests in
install_secret_scanners_test.py): skip when the marker exists, write the
marker only on success, leave it absent on failure so the next boot retries.
Each test sources the script in a fresh bash process (it only runs `main`
when executed directly) with test-controlled overrides (see
bootstrap.testing for the stub `curl`/`uname` mechanics).
"""

from __future__ import annotations

from pathlib import Path

from bootstrap.testing import REPO_ROOT
from bootstrap.testing import install_fake_pinned_scanner
from bootstrap.testing import make_scanner_tarball
from bootstrap.testing import make_stub_bin
from bootstrap.testing import run_sourced

_SCRIPT = REPO_ROOT / "scripts" / "deferred_install.sh"
_SHARED_INSTALLER = REPO_ROOT / "scripts" / "install_secret_scanners.sh"


def test_install_secret_scanner_skips_when_marker_present(tmp_path: Path) -> None:
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    (marker_dir / "done.betterleaks").write_text("")
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=None, arch="x86_64")
    install_dir = tmp_path / "install"

    result = run_sourced(
        _SCRIPT,
        "_install_secret_scanner betterleaks",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "SECRET_SCANNER_INSTALL_DIR": str(install_dir),
        },
        stub_bin=stub_bin,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "skipping" in result.stdout
    assert not curl_log.exists()  # never even attempted a download
    assert not (install_dir / "betterleaks").exists()


def test_install_secret_scanner_writes_marker_when_binary_already_pinned(
    tmp_path: Path,
) -> None:
    # The docker-image case: the binary was baked in at the pinned version, so
    # the shared installer no-ops (no download) and the wrapper's only job is
    # to write the marker.
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    install_dir = tmp_path / "install"
    install_fake_pinned_scanner(install_dir, _SHARED_INSTALLER, "kingfisher")
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=None, arch="x86_64")

    result = run_sourced(
        _SCRIPT,
        "_install_secret_scanner kingfisher",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "SECRET_SCANNER_INSTALL_DIR": str(install_dir),
        },
        stub_bin=stub_bin,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "marker written" in result.stdout
    assert (marker_dir / "done.kingfisher").exists()
    assert not curl_log.exists()  # no download needed


def test_install_secret_scanner_fails_without_marker_on_checksum_mismatch(
    tmp_path: Path,
) -> None:
    # The stub curl serves a tarball that cannot match the pinned sha256, so
    # the shared installer must fail, install nothing, and the wrapper must
    # leave the marker unwritten (the next boot retries).
    tarball = tmp_path / "trufflehog.tar.gz"
    make_scanner_tarball(tarball, "trufflehog")
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=tarball, arch="x86_64")
    install_dir = tmp_path / "install"

    result = run_sourced(
        _SCRIPT,
        "_install_secret_scanner trufflehog",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "SECRET_SCANNER_INSTALL_DIR": str(install_dir),
        },
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "sha256 MISMATCH" in result.stdout
    assert "marker not written" in result.stdout
    assert curl_log.exists()  # the download itself did happen
    assert not (marker_dir / "done.trufflehog").exists()
    assert not (install_dir / "trufflehog").exists()


def test_install_secret_scanner_fails_without_marker_on_unsupported_arch(
    tmp_path: Path,
) -> None:
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=None, arch="mips64")
    install_dir = tmp_path / "install"

    result = run_sourced(
        _SCRIPT,
        "_install_secret_scanner betterleaks",
        extra_env={
            "DEFERRED_INSTALL_MARKER_DIR": str(marker_dir),
            "SECRET_SCANNER_INSTALL_DIR": str(install_dir),
        },
        stub_bin=stub_bin,
    )
    assert result.returncode != 0
    assert "no pinned binary for architecture 'mips64'" in result.stdout
    assert "marker not written" in result.stdout
    assert not curl_log.exists()
    assert not (marker_dir / "done.betterleaks").exists()
