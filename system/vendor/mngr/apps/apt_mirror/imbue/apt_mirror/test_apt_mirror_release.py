"""Live end-to-end proof of the deployed mirror: a real trixie container runs
apt against https://apt.imbuepackages.com at the committed cut timestamp.

Release-only: it depends on the deployed Worker, the R2 bucket, and a cut of
the committed timestamp, none of which per-PR CI should be coupled to.
"""

import subprocess

import pytest

from imbue.apt_mirror.cli import CURRENT_TIMESTAMP_PATH
from imbue.apt_mirror.cli import read_current_timestamp

_LIVE_MIRROR_BASE_URL = "https://apt.imbuepackages.com"


def _pinned_sources_script(timestamp: str) -> str:
    """A container script mirroring dwt's write_apt_sources.sh: pin sources, update, install."""
    debian_uri = f"{_LIVE_MIRROR_BASE_URL}/snap/{timestamp}/debian"
    security_uri = f"{_LIVE_MIRROR_BASE_URL}/snap/{timestamp}/debian-security"
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
            "apt-get install -y --no-install-recommends jq",
            "jq --version",
        ]
    )


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.timeout(600)
def test_live_mirror_serves_apt_update_and_install_in_trixie_container() -> None:
    timestamp = read_current_timestamp(CURRENT_TIMESTAMP_PATH)
    result = subprocess.run(
        ["docker", "run", "--rm", "python:3.12-slim-trixie", "bash", "-c", _pinned_sources_script(timestamp)],
        capture_output=True,
        text=True,
        timeout=540,
    )
    assert result.returncode == 0, (
        f"apt against the live mirror failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "jq-" in result.stdout
