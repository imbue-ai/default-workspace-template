"""``setup_system.sh`` is the only path that installs a global binary.

The Dockerfile RUNs it at image build, the Lima provider runs it in the VM,
and the update apply re-runs it live when one of its inputs changed. An
installer wired into the Dockerfile as its own layer instead runs on exactly
one of those paths: an already-running workspace that takes an update naming
the binary never gets it (a service starts, fails to find it, and retry-loops),
and the same goes for any provider without a Docker build.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[1]
_DOCKERFILE_PATH = _REPO_ROOT / "system" / "Dockerfile"
_SCRIPTS_DIR = _REPO_ROOT / "system" / "scripts"
_SETUP_SYSTEM_PATH = _SCRIPTS_DIR / "setup_system.sh"

# The one installer the Dockerfile may run on its own: it writes INTO the
# workspace checkout (.venv, node_modules), which a create re-materializes and
# so must never be guarded or re-run as global provisioning (see
# _provision_guard.sh).
_IN_REPO_INSTALLER = "install_dependencies.sh"


def _dockerfile_installer_runs() -> list[str]:
    """The ``default-workspace-template-install-*`` commands the Dockerfile RUNs."""
    return re.findall(
        r"^RUN\b.*?\b(default-workspace-template-install-[\w-]+)\s*$",
        _DOCKERFILE_PATH.read_text(),
        re.MULTILINE,
    )


def test_dockerfile_runs_no_global_installer_outside_setup_system() -> None:
    allowed = {"default-workspace-template-install-dependencies"}
    assert set(_dockerfile_installer_runs()) <= allowed


def test_every_global_installer_script_is_invoked_by_setup_system() -> None:
    setup_system = _SETUP_SYSTEM_PATH.read_text()
    installers = sorted(
        path.name
        for path in _SCRIPTS_DIR.glob("*install*.sh")
        if path.name != _IN_REPO_INSTALLER
    )
    assert installers != []
    missing = [name for name in installers if name not in setup_system]
    assert missing == []


def test_every_version_pin_setup_system_expands_has_a_default() -> None:
    """A pin with no ``:=`` default has no source at all now that the Dockerfile
    carries none, and the script runs under ``set -u``."""
    setup_system = _SETUP_SYSTEM_PATH.read_text()
    expanded = set(re.findall(r"\$\{(\w+_VERSION)\}", setup_system))
    defaulted = set(re.findall(r'^: "\$\{(\w+_VERSION):=', setup_system, re.MULTILINE))
    assert expanded != set()
    assert expanded <= defaulted
