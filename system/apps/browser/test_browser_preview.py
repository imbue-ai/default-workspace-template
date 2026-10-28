"""The browser's preview boots beside the live service without touching its ports or its profiles, and taking it
down leaves the shared pulse daemon up with none of its browsers' audio sinks in it.

This checkout's ``browser-service`` is booted by update-app's ``preview_app.py`` from the manifest's
``[preview]`` table, while the live service's ports are taken and a live profile sits under the live host dir.
"""

from __future__ import annotations

import contextlib
import importlib.util
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
from browser import session as bsession
from imbue.mngr.utils.polling import wait_for

_APP_DIR = Path(__file__).parent
_CHECKOUT = _APP_DIR.parents[2]

_PREVIEW_SCRIPT = _CHECKOUT / ".agents" / "skills" / "update-app" / "scripts" / "preview_app.py"
_spec = importlib.util.spec_from_file_location("preview_app", _PREVIEW_SCRIPT)
assert _spec is not None and _spec.loader is not None
preview_app = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = preview_app
_spec.loader.exec_module(preview_app)

_APP_NAME = "browser"

# The live service's own port and its CDP proxy's (runner.py).
_LIVE_PORTS = (8081, 8083)

# serve_isolated_instance.py's --copy refuses a copy that would leave the disk under its reserve, and in a
# workspace pytest's temp root is the small RAM-backed /tmp.
_DISK_TEMP_ROOT = Path("/var/tmp")

_LIVE_PROFILE = "browser-use-user-data-dir-browser-1"
_LIVE_FLEET = '{"version": 2, "browsers": []}'

# The shared script's health probe makes up to 60 one-second attempts, for the instance and again for its preview
# frame; the restore and the teardown each get as long.
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


@pytest.fixture
def live_repo_root(repo_root_on_disk: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A live repo with an empty saved fleet, and a live host dir holding one profile, beside it on disk."""
    repo_root = repo_root_on_disk / "workspace"
    # The shared script registers the preview's services by running the live repo's registration script.
    (repo_root / "system" / "scripts").mkdir(parents=True)
    shutil.copy(_CHECKOUT / "system" / "scripts" / "forward_port.py", repo_root / "system" / "scripts")
    monkeypatch.setenv("MINDS_APPS_FILE", str(repo_root_on_disk / "apps.toml"))
    live_data = repo_root / "data" / ".apps" / "browser"
    live_data.mkdir(parents=True)
    # An empty saved fleet: a restore over it sweeps every profile under its root that it does not name.
    (live_data / "instances.json").write_text(_LIVE_FLEET)
    live_host_dir = repo_root_on_disk / "host"
    live_profile = live_host_dir / "browser-profiles" / _LIVE_PROFILE
    live_profile.mkdir(parents=True)
    (live_profile / "Cookies").write_text("the user's logins")
    # What the instance inherits in a workspace: the live host dir, under which the live profiles sit.
    monkeypatch.setenv("MNGR_HOST_DIR", str(live_host_dir))
    # Lets the restore run where Chromium is not installed; an empty fleet launches no Chromium anyway.
    monkeypatch.setenv("BROWSER_SKIP_INSTALL_CHECK", "1")
    # A port nothing listens on, so the preview's window sweep reaches no shell.
    monkeypatch.setenv("MINDS_WORKSPACE_SERVER_URL", "http://127.0.0.1:1")
    return repo_root


@contextlib.contextmanager
def _preview_beside_the_live_service(repo_root: Path) -> Generator[dict[str, int]]:
    """Boot the browser's preview while the live service's ports are taken, yield its ports once its restore has
    finished, and take it down on the way out."""
    health_path = load_manifest(_APP_DIR / "app.toml").preview.health_path
    instance_state = (
        repo_root / preview_app.INSTANCES_ROOT / preview_app.instance_name(_APP_NAME) / preview_app.INSTANCE_STATE_FILENAME
    )
    repo_args = ["--repo-root", str(repo_root)]
    with _held(_LIVE_PORTS[0]), _held(_LIVE_PORTS[1]):
        try:
            assert preview_app.main(["up", "--app", _APP_NAME, "--worktree", str(_CHECKOUT), *repo_args]) == 0
            ports = json.loads(instance_state.read_text())["ports"]
            wait_for(
                lambda: not _get_json(f"http://127.0.0.1:{ports['main']}{health_path}")["initializing"],
                timeout=_STEP_TIMEOUT_SECONDS,
                poll_interval=0.5,
                error_message="the preview's restore never finished",
            )
            yield ports
        finally:
            down_code = preview_app.main(["down", "--app", _APP_NAME, *repo_args])
    assert down_code == 0
    assert preview_app.live_preview_url(repo_root, _APP_NAME) is None


def _pulse_sinks() -> list[str]:
    # The one PulseAudio daemon every browser daemon in the workspace loads its sinks into.
    listed = subprocess.run(
        ["pactl", "list", "short", "sinks"],
        env=bsession._pulse_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.split("\t")[1] for line in listed.stdout.splitlines() if line.strip()]


def _pulse_daemon_answers() -> bool:
    probe = subprocess.run(["pactl", "info"], env=bsession._pulse_env(), capture_output=True, timeout=10)
    return probe.returncode == 0


@pytest.mark.timeout(5 * _STEP_TIMEOUT_SECONDS)
def test_a_preview_boots_on_its_own_ports_and_leaves_the_live_profiles_alone(live_repo_root: Path) -> None:
    with _preview_beside_the_live_service(live_repo_root) as ports:
        assert set(ports.values()).isdisjoint(_LIVE_PORTS), ports
        # The CDP proxy moves to an ephemeral port when its own is taken, so only a listener on the cdp port
        # shows the preview was given that port.
        socket.create_connection(("127.0.0.1", ports["cdp"]), timeout=5).close()

    live_profile = live_repo_root.parent / "host" / "browser-profiles" / _LIVE_PROFILE
    assert (live_profile / "Cookies").read_text() == "the user's logins"
    assert (live_repo_root / "data" / ".apps" / "browser" / "instances.json").read_text() == _LIVE_FLEET


@pytest.mark.browser
@pytest.mark.timeout(6 * _STEP_TIMEOUT_SECONDS)
def test_taking_a_preview_down_unloads_the_audio_sinks_of_its_browsers(live_repo_root: Path) -> None:
    with _preview_beside_the_live_service(live_repo_root) as ports:
        preview_url = f"http://127.0.0.1:{ports['main']}"
        request = urllib.request.Request(
            f"{preview_url}/browsers",
            data=json.dumps({"name": "preview-audio"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            name = json.loads(response.read())["name"]
        wait_for(
            lambda: _lifecycle_of(preview_url, name) != "init",
            timeout=_STEP_TIMEOUT_SECONDS,
            poll_interval=0.5,
            error_message=f"the preview's browser {name} never left init",
        )
        if _lifecycle_of(preview_url, name) != "running":
            pytest.skip("Chromium did not come up in this environment (Fortress not installed?)")
        sink_prefix = f"browserpreview{ports['main']}_"
        assert [sink for sink in _pulse_sinks() if sink.startswith(sink_prefix)], _pulse_sinks()

    # The live browsers play through the same daemon, so the preview's teardown must leave it running.
    assert _pulse_daemon_answers(), "the shared pulse daemon went down with the preview"
    assert not [sink for sink in _pulse_sinks() if sink.startswith(sink_prefix)], _pulse_sinks()


def _lifecycle_of(preview_url: str, name: str) -> str | None:
    """The browser's lifecycle, or None once the service no longer lists it, which is what a failed launch does."""
    browsers = _get_json(f"{preview_url}/browsers")["browsers"]
    assert isinstance(browsers, list)
    return next((str(browser["lifecycle"]) for browser in browsers if browser["id"] == name), None)
