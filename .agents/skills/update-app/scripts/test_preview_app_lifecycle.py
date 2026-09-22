"""A preview's whole lifecycle through the real shared script and the real registration script.

Run via: ``uv run pytest .agents/skills/update-app/scripts/test_preview_app_lifecycle.py``

``preview_app_test.py`` pins the exact commands this script hands the shared
``serve_isolated_instance.py``; here nothing stands in for it. A fixture app is
booted from a worktree with its manifest's ``[preview]`` table, rebuilt and
refreshed, and torn down, so a change to the shared script's flags, its state
file, or its registrations that ``preview_app.py`` has not kept up with fails
here even though both scripts' own suites still pass.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "preview_app.py"
_spec = importlib.util.spec_from_file_location("preview_app", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

# The shared script gives each boot up to 60s to pass its health check, and this test boots
# twice (``up``, then ``refresh``) before tearing down.
_BOOT_TIMEOUT_SECONDS = 180

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'

_MANIFEST = """
name = "fixture"
display_name = "Fixture"
icon = "icon.svg"

[preview]
command = ["python3", "system/apps/fixture/serve.py"]
env = {FIXTURE_PORT = "{port:main}", FIXTURE_DATA_DIR = "{copy:data}"}
copies = {data = "data/.apps/fixture"}
health_path = "/health"
"""

# Reads its build once at boot, so only a re-boot shows a rebuild; and writes into its
# data directory, which a preview must only ever see as a copy.
_SERVER = """
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

data_dir = Path(os.environ["FIXTURE_DATA_DIR"])
build = Path("system/apps/fixture/build.txt").read_text()
(data_dir / "written-by-preview.txt").write_text("preview")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(
            {"build": build, "greeting": (data_dir / "greeting.txt").read_text(), "pid": os.getpid()}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", int(os.environ["FIXTURE_PORT"])), Handler).serve_forever()
"""


def _get(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


def _registered_rows(registry: Path) -> dict[str, dict[str, object]]:
    if not registry.exists():
        return {}
    return {
        row["name"]: row for row in tomllib.loads(registry.read_text()).get("apps", [])
    }


@pytest.mark.timeout(_BOOT_TIMEOUT_SECONDS)
def test_a_preview_boots_from_its_manifest_refreshes_a_rebuild_in_place_and_tears_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "live"
    (repo_root / "system" / "scripts").mkdir(parents=True)
    shutil.copy(
        mod._FORWARD_PORT_SCRIPT, repo_root / "system" / "scripts" / "forward_port.py"
    )
    live_data = repo_root / "data" / ".apps" / "fixture"
    live_data.mkdir(parents=True)
    (live_data / "greeting.txt").write_text("from the live data")
    registry = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))

    worktree = tmp_path / "worktree"
    app_dir = worktree / "system" / "apps" / "fixture"
    app_dir.mkdir(parents=True)
    (app_dir / "app.toml").write_text(_MANIFEST)
    (app_dir / "icon.svg").write_text(_ICON)
    (app_dir / "serve.py").write_text(_SERVER)
    (app_dir / "build.txt").write_text("first build")

    repo_args = ["--repo-root", str(repo_root)]
    assert (
        mod.main(["up", "--app", "fixture", "--worktree", str(worktree), *repo_args])
        == 0
    )
    try:
        url = mod.live_preview_url(repo_root, "fixture")
        assert url is not None
        first = _get(url)
        assert first["build"] == "first build"
        assert first["greeting"] == "from the live data"
        assert not (live_data / "written-by-preview.txt").exists()
        rows = _registered_rows(registry)
        assert rows["fixture-preview-app"]["url"] == url.replace(
            "127.0.0.1", "localhost"
        )
        assert rows["fixture-preview-app"]["internal"] is True
        assert rows["fixture-preview"]["display_name"] == "Fixture (worktree)"

        (app_dir / "build.txt").write_text("second build")
        assert _get(url)["build"] == "first build"
        assert mod.main(["refresh", "--app", "fixture", *repo_args]) == 0
        second = _get(url)
        assert second["build"] == "second build"
        assert second["pid"] != first["pid"]
    finally:
        down_code = mod.main(["down", "--app", "fixture", *repo_args])

    assert down_code == 0
    with pytest.raises(urllib.error.URLError):
        _get(url)
    assert (
        _registered_rows(registry)
        .keys()
        .isdisjoint({"fixture-preview-app", "fixture-preview"})
    )
    assert mod.live_preview_url(repo_root, "fixture") is None
