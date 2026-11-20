"""Tests for install_grok.sh, run against a stub `curl` and `uname` so none reaches the network."""

import gzip
import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from bootstrap.testing import make_stub_bin

_SCRIPT = Path(__file__).with_name("install_grok.sh")
(_PINNED_VERSION,) = re.findall(
    r'^GROK_VERSION="([^"]+)"$', _SCRIPT.read_text(), re.MULTILINE
)
_PINNED_SHA256_LINE = re.compile(r'^(\s*sha256=")[0-9a-f]{64}"$', re.MULTILINE)


def _write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def _fake_grok_script(version: str) -> str:
    return f'#!/bin/sh\necho "grok {version} (0000000)"\n'


def _script_pinned_to(tmp_path: Path, served: Path) -> Path:
    """A copy of the installer whose pinned sha256s are ``served``'s."""
    digest = hashlib.sha256(served.read_bytes()).hexdigest()
    text, count = _PINNED_SHA256_LINE.subn(rf'\g<1>{digest}"', _SCRIPT.read_text())
    assert count == 2
    copy = tmp_path / _SCRIPT.name
    copy.write_text(text)
    return copy


def _run(
    tmp_path: Path,
    install_path: Path,
    *,
    arch: str,
    served: Path | None,
    script: Path = _SCRIPT,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the installer against stub `curl` and `uname`; returns the result and the curl log."""
    stub_bin = tmp_path / "stub-bin"
    curl_log = make_stub_bin(stub_bin, served_tarball=served, arch=arch)
    result = subprocess.run(
        ["bash", str(script)],
        env={
            "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "GROK_INSTALL_PATH": str(install_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, curl_log


def test_a_grok_at_the_pinned_version_is_kept_without_a_download(
    tmp_path: Path,
) -> None:
    install_path = _write_executable(
        tmp_path / "bin" / "grok", _fake_grok_script(_PINNED_VERSION)
    )

    result, curl_log = _run(tmp_path, install_path, arch="x86_64", served=None)

    assert result.returncode == 0, result.stderr
    assert not curl_log.exists()
    assert install_path.read_text() == _fake_grok_script(_PINNED_VERSION)


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_a_download_that_does_not_match_the_pinned_sha256_is_refused(
    tmp_path: Path, arch: str
) -> None:
    install_path = _write_executable(
        tmp_path / "bin" / "grok", _fake_grok_script("0.0.1")
    )
    served = tmp_path / "served.gz"
    served.write_bytes(gzip.compress(_fake_grok_script(_PINNED_VERSION).encode()))

    result, curl_log = _run(tmp_path, install_path, arch=arch, served=served)

    assert result.returncode != 0
    assert "does not match its pinned sha256" in result.stderr
    assert (
        f"https://x.ai/cli/grok-{_PINNED_VERSION}-linux-{arch}.gz"
        in curl_log.read_text()
    )
    assert install_path.read_text() == _fake_grok_script("0.0.1")
    assert [path.name for path in install_path.parent.iterdir()] == ["grok"]


@pytest.mark.parametrize(
    ("served_version", "expected_returncode", "expected_version"),
    [(_PINNED_VERSION, 0, _PINNED_VERSION), ("0.0.2", 1, "0.0.1")],
)
def test_a_verified_download_replaces_grok_only_when_it_reports_the_pinned_version(
    tmp_path: Path, served_version: str, expected_returncode: int, expected_version: str
) -> None:
    install_path = _write_executable(
        tmp_path / "bin" / "grok", _fake_grok_script("0.0.1")
    )
    served = tmp_path / "served.gz"
    served.write_bytes(gzip.compress(_fake_grok_script(served_version).encode()))

    result, _ = _run(
        tmp_path,
        install_path,
        arch="aarch64",
        served=served,
        script=_script_pinned_to(tmp_path, served),
    )

    assert result.returncode == expected_returncode, result.stderr
    assert install_path.read_text() == _fake_grok_script(expected_version)
    assert [path.name for path in install_path.parent.iterdir()] == ["grok"]
