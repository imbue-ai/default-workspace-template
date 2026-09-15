import hashlib
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds_admin.slices.onetun_install import ONETUN_VERSION
from imbue.minds_admin.slices.onetun_install import OnetunInstallError
from imbue.minds_admin.slices.onetun_install import install_pinned_onetun
from imbue.minds_admin.slices.onetun_install import installed_onetun_version_or_none
from imbue.minds_admin.slices.onetun_install import parse_onetun_version_output
from imbue.minds_admin.slices.onetun_install import select_onetun_release_asset
from imbue.minds_admin.slices.onetun_install import well_known_onetun_path
from imbue.minds_admin.slices.onetun_install import write_verified_onetun_binary


def _asset_and_digest(system: str, machine: str) -> tuple[str, str]:
    artifact = select_onetun_release_asset(system, machine)
    return artifact.subpath, artifact.digest


def test_select_onetun_release_asset_covers_every_supported_operator_platform() -> None:
    # The exact pinned assets and hashes: a version bump must consciously
    # recompute all three from the new release's official binaries.
    assert _asset_and_digest("Linux", "x86_64") == snapshot(
        ("onetun-linux-amd64", "1c2918da3ee3b3d0f522aa82d29c194741593737df85a73b0c6ce15502c00d15")
    )
    assert _asset_and_digest("Linux", "aarch64") == snapshot(
        ("onetun-linux-aarch64", "1cb94c47da4bffe622d5efe4ffb5ba3bde8e4f89c2362c3b99b69df483fd728a")
    )
    assert _asset_and_digest("Darwin", "arm64") == snapshot(
        ("onetun-macos-aarch64", "944911ac292590f5be763aa5f99ba47256d4ff33746e4f5831bc79ece343a4af")
    )


def test_select_onetun_release_asset_refuses_platforms_without_a_prebuilt_binary() -> None:
    # macOS Intel has no asset in the pinned release; the error must point at
    # the MNGR_ONETUN_PATH escape hatch.
    with pytest.raises(OnetunInstallError, match="MNGR_ONETUN_PATH"):
        select_onetun_release_asset("Darwin", "x86_64")


def test_select_onetun_release_asset_points_at_the_mirrored_pinned_release() -> None:
    assert select_onetun_release_asset("Linux", "x86_64").mirror_url == snapshot(
        "https://apt.imbuepackages.com/artifacts/onetun/0.3.10/onetun-linux-amd64"
    )


def test_parse_onetun_version_output_reads_the_version_line() -> None:
    assert parse_onetun_version_output("onetun 0.3.10\n") == "0.3.10"


def test_parse_onetun_version_output_returns_none_for_unexpected_output() -> None:
    assert parse_onetun_version_output("") is None
    assert parse_onetun_version_output("something else entirely") is None


def test_write_verified_onetun_binary_installs_an_executable_on_hash_match(tmp_path: Path) -> None:
    data = b"#!/bin/sh\necho fake-onetun-58311\n"
    destination = tmp_path / "bin" / "onetun"

    write_verified_onetun_binary(data, hashlib.sha256(data).hexdigest(), destination)

    assert destination.read_bytes() == data
    assert destination.stat().st_mode & 0o111


def test_write_verified_onetun_binary_refuses_a_hash_mismatch_without_writing(tmp_path: Path) -> None:
    destination = tmp_path / "bin" / "onetun"

    with pytest.raises(OnetunInstallError, match="hash mismatch"):
        write_verified_onetun_binary(b"tampered bytes", "0" * 64, destination)

    assert not destination.exists()


def _write_stub_binary(path: Path, stdout_line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{stdout_line}'\n")
    path.chmod(0o755)


def test_installed_onetun_version_or_none_reads_a_live_binary(tmp_path: Path) -> None:
    binary_path = tmp_path / "onetun"
    _write_stub_binary(binary_path, "onetun 0.3.10")

    with ConcurrencyGroup(name="onetun-version-test") as concurrency_group:
        assert installed_onetun_version_or_none(binary_path, concurrency_group) == "0.3.10"


def test_installed_onetun_version_or_none_is_none_for_a_missing_binary(tmp_path: Path) -> None:
    with ConcurrencyGroup(name="onetun-version-test") as concurrency_group:
        assert installed_onetun_version_or_none(tmp_path / "absent", concurrency_group) is None


def test_installed_onetun_version_or_none_is_none_for_a_failing_binary(tmp_path: Path) -> None:
    binary_path = tmp_path / "onetun"
    binary_path.write_text("#!/bin/sh\nexit 41\n")
    binary_path.chmod(0o755)

    with ConcurrencyGroup(name="onetun-version-test") as concurrency_group:
        assert installed_onetun_version_or_none(binary_path, concurrency_group) is None


def test_installed_onetun_version_or_none_is_none_for_an_unspawnable_binary(tmp_path: Path) -> None:
    # A file that cannot be executed at all (no exec bit; same shape as a
    # wrong-architecture binary) must read as "refresh me", not crash.
    binary_path = tmp_path / "onetun"
    binary_path.write_text("not a program")

    with ConcurrencyGroup(name="onetun-version-test") as concurrency_group:
        assert installed_onetun_version_or_none(binary_path, concurrency_group) is None


def test_install_pinned_onetun_no_ops_when_the_pinned_version_is_already_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An already-current binary must short-circuit before any network access
    # (this test would fail on a download attempt: no network in unit tests).
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_stub_binary(well_known_onetun_path(), f"onetun {ONETUN_VERSION}")

    with ConcurrencyGroup(name="onetun-install-test") as concurrency_group:
        result = install_pinned_onetun(concurrency_group)

    assert result.was_already_current is True
    assert result.version == ONETUN_VERSION
    assert result.binary_path == well_known_onetun_path()
