"""Live end-to-end proof of the mirror's frozen docker archive: a real trixie
container installs the pinned docker-ce from https://apt.imbuepackages.com at
the committed cut timestamp, verifying it with the committed signing key.

Release-only: it depends on the deployed Worker, a cut + warm of the committed
timestamp that covered the ``docker`` archive, and Docker locally.
"""

import subprocess

import pytest

from imbue.apt_mirror.cli import CURRENT_TIMESTAMP_PATH
from imbue.apt_mirror.cli import read_current_timestamp
from imbue.minds_admin.slices.bare_metal_prep import render_docker_apt_source_section
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION_CORE


def _docker_install_script(timestamp: str) -> str:
    """A container script running the gen-2 guest customization's docker apt-source setup against the live mirror."""
    pinned_version = f"{PINNED_DOCKER_APT_VERSION_CORE}~debian.13~trixie"
    return "\n".join(
        [
            "set -euo pipefail",
            "export DEBIAN_FRONTEND=noninteractive",
            # Only the mirror's docker archive is configured, so a missing or
            # unsigned index fails the update instead of falling back anywhere.
            "rm -f /etc/apt/sources.list.d/debian.sources",
            ": > /etc/apt/sources.list",
            render_docker_apt_source_section(timestamp),
            "apt-get update",
            f"apt-get download docker-ce-cli={pinned_version}",
            "ls docker-ce-cli_*.deb",
        ]
    )


@pytest.mark.release
@pytest.mark.docker
@pytest.mark.timeout(600)
def test_live_mirror_serves_the_pinned_docker_engine_to_a_trixie_container() -> None:
    timestamp = read_current_timestamp(CURRENT_TIMESTAMP_PATH)
    result = subprocess.run(
        ["docker", "run", "--rm", "python:3.12-slim-trixie", "bash", "-c", _docker_install_script(timestamp)],
        capture_output=True,
        text=True,
        timeout=540,
    )
    assert result.returncode == 0, (
        f"docker-ce-cli from the live mirror failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "docker-ce-cli_" in result.stdout
