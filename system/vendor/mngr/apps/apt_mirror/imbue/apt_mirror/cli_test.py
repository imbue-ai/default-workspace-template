from pathlib import Path

import pytest
from click.testing import CliRunner

from imbue.apt_mirror.cli import CURRENT_TIMESTAMP_PATH
from imbue.apt_mirror.cli import PACKAGE_LISTS_DIR
from imbue.apt_mirror.cli import main
from imbue.apt_mirror.cli import read_current_timestamp
from imbue.apt_mirror.mock_apt_mirror_test import InMemoryAptMirrorStorage
from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher
from imbue.apt_mirror.package_lists import read_package_lists
from imbue.apt_mirror.parsing import pool_cache_key
from imbue.apt_mirror.service import AptMirrorService
from imbue.apt_mirror.testing import FOO_PACKAGES_TEXT
from imbue.apt_mirror.testing import compress_packages_index
from imbue.apt_mirror.testing import wire_canned_suite

TIMESTAMP = "20260725T000000Z"
_SNAPSHOT_DEBIAN = f"https://snapshot.debian.org/archive/debian/{TIMESTAMP}"
_SNAPSHOT_SECURITY = f"https://snapshot.debian.org/archive/debian-security/{TIMESTAMP}"


def _service_with_canned_archives(packages_text: str) -> tuple[AptMirrorService, InMemoryAptMirrorStorage]:
    """A service whose fetcher serves a minimal amd64+arm64 archive pair for the default suites."""
    fetcher = MappingUpstreamFetcher()
    storage = InMemoryAptMirrorStorage()
    packages_xz = compress_packages_index(packages_text)
    empty_xz = compress_packages_index("")
    for base, suites in ((_SNAPSHOT_DEBIAN, ("trixie", "trixie-updates")), (_SNAPSHOT_SECURITY, ("trixie-security",))):
        for suite in suites:
            wire_canned_suite(fetcher, base, suite, {"amd64": packages_xz, "arm64": empty_xz})
    return AptMirrorService(storage=storage, fetcher=fetcher), storage


def _write_warm_inputs(tmp_path: Path, list_text: str) -> tuple[Path, Path]:
    """Write the current-timestamp and package list files warm/verify read; returns (timestamp_file, list_file)."""
    timestamp_file = tmp_path / "current-timestamp"
    timestamp_file.write_text(f"{TIMESTAMP}\n")
    list_file = tmp_path / "list.txt"
    list_file.write_text(list_text)
    return timestamp_file, list_file


def _add_foo_pool_deb(service: AptMirrorService) -> None:
    """Serve foo's pool deb from the live archive on the service's canned fetcher."""
    fetcher = service.fetcher
    assert isinstance(fetcher, MappingUpstreamFetcher)
    fetcher.responses_by_url["https://deb.debian.org/debian/pool/main/f/foo/foo_1.0_amd64.deb"] = b"foo-deb"


def test_committed_current_timestamp_parses() -> None:
    # The committed value changes with every cut, so assert validity (the read
    # raises on any malformed content) rather than pinning the current value.
    assert read_current_timestamp(CURRENT_TIMESTAMP_PATH)


def test_committed_package_lists_parse_and_are_nonempty() -> None:
    names = read_package_lists(sorted(PACKAGE_LISTS_DIR.glob("*.txt")))
    assert len(names) > 20
    assert "git" in names


def test_cut_writes_timestamp_file_and_reports_counts(tmp_path: Path) -> None:
    service, _storage = _service_with_canned_archives(FOO_PACKAGES_TEXT)
    timestamp_file = tmp_path / "current-timestamp"

    result = CliRunner().invoke(
        main,
        ["cut", "--timestamp", TIMESTAMP, "--timestamp-file", str(timestamp_file)],
        obj=service,
    )

    assert result.exit_code == 0, result.output
    assert "stored" in result.output
    assert timestamp_file.read_text() == f"{TIMESTAMP}\n"


def test_cut_skip_timestamp_file_leaves_file_untouched(tmp_path: Path) -> None:
    service, _storage = _service_with_canned_archives(FOO_PACKAGES_TEXT)
    timestamp_file = tmp_path / "current-timestamp"

    result = CliRunner().invoke(
        main,
        ["cut", "--timestamp", TIMESTAMP, "--timestamp-file", str(timestamp_file), "--skip-timestamp-file"],
        obj=service,
    )

    assert result.exit_code == 0, result.output
    assert not timestamp_file.exists()


def test_warm_exits_zero_when_complete(tmp_path: Path) -> None:
    service, storage = _service_with_canned_archives(FOO_PACKAGES_TEXT)
    _add_foo_pool_deb(service)
    timestamp_file, list_file = _write_warm_inputs(tmp_path, "foo\n")
    runner = CliRunner()
    cut_result = runner.invoke(
        main, ["cut", "--timestamp", TIMESTAMP, "--timestamp-file", str(timestamp_file)], obj=service
    )
    assert cut_result.exit_code == 0, cut_result.output

    result = runner.invoke(
        main,
        ["warm", "--timestamp-file", str(timestamp_file), "--list", str(list_file)],
        obj=service,
    )

    assert result.exit_code == 0, result.output
    assert "fetched 1" in result.output
    assert storage.get_object(pool_cache_key("debian", "main/f/foo/foo_1.0_amd64.deb")) == b"foo-deb"


def test_warm_exits_nonzero_on_unknown_package(tmp_path: Path) -> None:
    service, _storage = _service_with_canned_archives(FOO_PACKAGES_TEXT)
    _add_foo_pool_deb(service)
    timestamp_file, list_file = _write_warm_inputs(tmp_path, "foo\nno-such-package\n")
    runner = CliRunner()
    runner.invoke(main, ["cut", "--timestamp", TIMESTAMP, "--timestamp-file", str(timestamp_file)], obj=service)

    result = runner.invoke(
        main,
        ["warm", "--timestamp-file", str(timestamp_file), "--list", str(list_file)],
        obj=service,
    )

    assert result.exit_code == 1
    assert "UNKNOWN PACKAGE" in result.output


def test_verify_exits_nonzero_before_warm_and_zero_after(tmp_path: Path) -> None:
    service, _storage = _service_with_canned_archives(FOO_PACKAGES_TEXT)
    _add_foo_pool_deb(service)
    timestamp_file, list_file = _write_warm_inputs(tmp_path, "foo\n")
    runner = CliRunner()
    runner.invoke(main, ["cut", "--timestamp", TIMESTAMP, "--timestamp-file", str(timestamp_file)], obj=service)

    before = runner.invoke(
        main, ["verify", "--timestamp-file", str(timestamp_file), "--list", str(list_file)], obj=service
    )
    assert before.exit_code == 1
    assert "MISSING" in before.output

    warm_result = runner.invoke(
        main, ["warm", "--timestamp-file", str(timestamp_file), "--list", str(list_file)], obj=service
    )
    assert warm_result.exit_code == 0, warm_result.output

    after = runner.invoke(
        main, ["verify", "--timestamp-file", str(timestamp_file), "--list", str(list_file)], obj=service
    )
    assert after.exit_code == 0, after.output


def test_verify_before_cut_reports_clean_error(tmp_path: Path) -> None:
    service = AptMirrorService(storage=InMemoryAptMirrorStorage(), fetcher=MappingUpstreamFetcher())
    timestamp_file, list_file = _write_warm_inputs(tmp_path, "foo\n")

    result = CliRunner().invoke(
        main, ["verify", "--timestamp-file", str(timestamp_file), "--list", str(list_file)], obj=service
    )

    assert result.exit_code == 2
    assert "has not been cut" in result.output


def test_warm_without_r2_configuration_reports_clean_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in (
        "APT_MIRROR_R2_ENDPOINT",
        "APT_MIRROR_R2_BUCKET",
        "APT_MIRROR_R2_ACCESS_KEY_ID",
        "APT_MIRROR_R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
    timestamp_file, list_file = _write_warm_inputs(tmp_path, "foo\n")

    result = CliRunner().invoke(main, ["warm", "--timestamp-file", str(timestamp_file), "--list", str(list_file)])

    assert result.exit_code == 2
    assert "APT_MIRROR_R2_ENDPOINT" in result.output


def test_missing_timestamp_file_reports_clean_error(tmp_path: Path) -> None:
    service = AptMirrorService(storage=InMemoryAptMirrorStorage(), fetcher=MappingUpstreamFetcher())
    _timestamp_file, list_file = _write_warm_inputs(tmp_path, "foo\n")
    missing_file = tmp_path / "no-such-timestamp-file"

    result = CliRunner().invoke(
        main, ["warm", "--timestamp-file", str(missing_file), "--list", str(list_file)], obj=service
    )

    assert result.exit_code == 2
    assert "Cannot read current-timestamp file" in result.output


def test_invalid_timestamp_reports_clean_error(tmp_path: Path) -> None:
    """Both cut and warm turn a malformed --timestamp into a one-line error and exit code 2."""
    service = AptMirrorService(storage=InMemoryAptMirrorStorage(), fetcher=MappingUpstreamFetcher())
    _timestamp_file, list_file = _write_warm_inputs(tmp_path, "foo\n")
    runner = CliRunner()

    cut_result = runner.invoke(main, ["cut", "--timestamp", "not-a-timestamp", "--skip-timestamp-file"], obj=service)
    assert cut_result.exit_code == 2
    assert "Invalid snapshot timestamp" in cut_result.output

    warm_result = runner.invoke(
        main, ["warm", "--timestamp", "not-a-timestamp", "--list", str(list_file)], obj=service
    )
    assert warm_result.exit_code == 2
    assert "Invalid snapshot timestamp" in warm_result.output
