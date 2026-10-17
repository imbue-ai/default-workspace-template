"""The browser's preview boots beside the live service without touching its ports or its profiles.

The real ``browser-service`` is booted through ``serve_isolated_instance.py`` from the manifest's
``[preview]`` table, the way update-self's isolated boot and update-app's preview boot it, while the live
service's ports are taken and a live profile sits under the live host dir.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest
from app_manifest.manifest import load_manifest
from imbue.mngr.utils.polling import wait_for

_APP_DIR = Path(__file__).parent
_SERVE_SCRIPT = _APP_DIR.parents[2] / ".agents" / "shared" / "scripts" / "serve_isolated_instance.py"
_INSTANCE_NAME = "browser-preview-test"
# Where serve_isolated_instance.py records the ports it gave the instance, under its repo root.
_INSTANCE_STATE = Path("data/.state/isolated-instances") / _INSTANCE_NAME / "instance.json"

# The live service's own port and its CDP proxy's (runner.py).
_LIVE_PORTS = (8081, 8083)

# serve_isolated_instance.py's --copy refuses a copy that would leave the disk under its reserve, and in a
# workspace pytest's temp root is the small RAM-backed /tmp.
_DISK_TEMP_ROOT = Path("/var/tmp")

_LIVE_PROFILE = "browser-use-user-data-dir-browser-1"

# The shared script's health probe makes up to 60 one-second attempts; the restore and the teardown each get as long.
_STEP_TIMEOUT_SECONDS = 60


@contextlib.contextmanager
def _held(port: int) -> Generator[None]:
    """Keep ``port`` taken for the duration; one already taken (a live browser in this workspace) stays so."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            yield
            return
        listener.listen()
        yield


def _get_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


@pytest.fixture
def repo_root_on_disk() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="browser-preview-", dir=_DISK_TEMP_ROOT) as directory:
        yield Path(directory)


@pytest.mark.timeout(4 * _STEP_TIMEOUT_SECONDS)
def test_a_preview_boots_on_its_own_ports_and_leaves_the_live_profiles_alone(repo_root_on_disk: Path) -> None:
    repo_root = repo_root_on_disk / "workspace"
    live_data = repo_root / "data" / ".apps" / "browser"
    live_data.mkdir(parents=True)
    # An empty saved fleet: a restore over it sweeps every profile under its root that it does not name.
    live_fleet = '{"version": 2, "browsers": []}'
    (live_data / "instances.json").write_text(live_fleet)
    live_host_dir = repo_root_on_disk / "host"
    live_profile = live_host_dir / "browser-profiles" / _LIVE_PROFILE
    live_profile.mkdir(parents=True)
    (live_profile / "Cookies").write_text("the user's logins")

    preview = load_manifest(_APP_DIR / "app.toml").preview
    command = shutil.which(preview.command[0])
    assert command is not None, f"{preview.command[0]!r} is not on PATH"
    repo_args = ["--repo-root", str(repo_root)]
    up_argv = [
        sys.executable,
        str(_SERVE_SCRIPT),
        "up",
        "--name",
        _INSTANCE_NAME,
        "--cwd",
        str(repo_root),
        *repo_args,
        "--health-path",
        preview.health_path,
        *(arg for port_name in preview.ports for arg in ("--port", str(port_name))),
        *(arg for key, value in preview.env.items() for arg in ("--env", f"{key}={value}")),
        *(arg for key, source in preview.copies.items() for arg in ("--copy", f"{key}={source}")),
        # What the instance inherits in a workspace: the live host dir, under which the live profiles sit.
        "--env",
        f"MNGR_HOST_DIR={live_host_dir}",
        # Lets the restore run where Chromium is not installed; an empty fleet launches no Chromium anyway.
        "--env",
        "BROWSER_SKIP_INSTALL_CHECK=1",
        # A port nothing listens on, so the preview's window sweep reaches no shell.
        "--env",
        "MINDS_WORKSPACE_SERVER_URL=http://127.0.0.1:1",
        "--",
        command,
        *preview.command[1:],
        *preview.args,
    ]

    with _held(_LIVE_PORTS[0]), _held(_LIVE_PORTS[1]):
        try:
            up = subprocess.run(
                up_argv, capture_output=True, text=True, timeout=2 * _STEP_TIMEOUT_SECONDS, check=False
            )
            assert up.returncode == 0, up.stderr
            url = up.stdout.strip()
            ports = json.loads((repo_root / _INSTANCE_STATE).read_text())["ports"]
            assert set(ports.values()).isdisjoint(_LIVE_PORTS), ports
            # The CDP proxy moves to an ephemeral port when its own is taken, so only a listener on the cdp port
            # shows the preview was given that port.
            socket.create_connection(("127.0.0.1", ports["cdp"]), timeout=5).close()
            wait_for(
                lambda: not _get_json(f"{url}{preview.health_path}")["initializing"],
                timeout=_STEP_TIMEOUT_SECONDS,
                poll_interval=0.5,
                error_message="the preview's restore never finished",
            )
        finally:
            down = subprocess.run(
                [sys.executable, str(_SERVE_SCRIPT), "down", "--name", _INSTANCE_NAME, *repo_args],
                capture_output=True,
                text=True,
                timeout=_STEP_TIMEOUT_SECONDS,
                check=False,
            )

    assert down.returncode == 0, down.stderr
    assert (live_profile / "Cookies").read_text() == "the user's logins"
    assert (live_data / "instances.json").read_text() == live_fleet
