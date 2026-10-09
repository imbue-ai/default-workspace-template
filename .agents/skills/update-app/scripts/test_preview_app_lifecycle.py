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
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "preview_app.py"
_spec = importlib.util.spec_from_file_location("preview_app", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

# The shared script gives each health wait up to 60 one-second attempts, and this test
# waits three times: the instance and its wrapper page at ``up``, the instance at ``refresh``.
_LIFECYCLE_TIMEOUT_SECONDS = 180

# The preview's --copy refuses a copy that would leave the disk under its reserve, and
# in a workspace pytest's temp root is the small RAM-backed /tmp, so the live repo whose
# data gets copied sits on disk, where a real workspace's copies land.
_DISK_TEMP_ROOT = Path("/var/tmp")

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


# A real worktree is a checkout of the workspace, whose root is a uv project the preview
# syncs before it boots; with apps that are packages of their own, a workspace over them.
_ROOT_PYPROJECT = """
[project]
name = "fixture-workspace"
version = "0.1.0"
requires-python = ">=3.11"

[tool.uv]
package = false
"""
_WORKSPACE_PYPROJECT = (
    _ROOT_PYPROJECT
    + """
[tool.uv.workspace]
members = ["system/apps/*"]
"""
)

# An app that runs as its own console script, like every scaffolded app: what serves is
# whichever install of ``fixture-serve`` the preview's command resolves to.
_PACKAGED_MANIFEST = """
name = "fixture"
display_name = "Fixture"
icon = "icon.svg"

[preview]
command = ["fixture-serve"]
env = {FIXTURE_PORT = "{port:main}"}
health_path = "/health"
"""

_PACKAGED_PYPROJECT = """
[project]
name = "fixture"
version = "0.1.0"
requires-python = ">=3.11"

[project.scripts]
fixture-serve = "fixture_app:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/fixture_app"]
"""

_PACKAGED_SERVER = """
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BUILD = "live build"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"build": BUILD, "path": os.environ["PATH"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
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


@pytest.fixture
def live_repo_root() -> Iterator[Path]:
    """A live repo root on disk, holding the registration script the preview registers through."""
    with tempfile.TemporaryDirectory(
        prefix="preview-lifecycle-", dir=_DISK_TEMP_ROOT
    ) as directory:
        repo_root = Path(directory) / "live"
        (repo_root / "system" / "scripts").mkdir(parents=True)
        shutil.copy(
            mod._FORWARD_PORT_SCRIPT,
            repo_root / "system" / "scripts" / "forward_port.py",
        )
        yield repo_root


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = tmp_path / "apps.toml"
    monkeypatch.setenv("MINDS_APPS_FILE", str(registry))
    return registry


@pytest.mark.timeout(_LIFECYCLE_TIMEOUT_SECONDS)
def test_a_preview_boots_from_its_manifest_refreshes_a_rebuild_in_place_and_tears_down(
    tmp_path: Path, live_repo_root: Path, registry: Path
) -> None:
    repo_root = live_repo_root
    live_data = repo_root / "data" / ".apps" / "fixture"
    live_data.mkdir(parents=True)
    (live_data / "greeting.txt").write_text("from the live data")

    worktree = tmp_path / "worktree"
    app_dir = worktree / "system" / "apps" / "fixture"
    app_dir.mkdir(parents=True)
    (worktree / "pyproject.toml").write_text(_ROOT_PYPROJECT)
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


@pytest.mark.timeout(_LIFECYCLE_TIMEOUT_SECONDS)
@pytest.mark.usefixtures("registry")
def test_a_preview_from_a_fresh_worktree_runs_the_worktrees_code_not_the_live_install(
    tmp_path: Path, live_repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live repo has the app installed in its venv, and ``preview_app.py`` runs under
    ``uv run`` from the live repo, which puts that venv on PATH. A fresh worktree's own venv
    has no ``fixture-serve`` until something installs every workspace member into it, and
    until then the live one answers for it and the preview serves the live code."""
    repo_root = live_repo_root
    app_dir = repo_root / "system" / "apps" / "fixture"
    (app_dir / "src" / "fixture_app").mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text(_WORKSPACE_PYPROJECT)
    (app_dir / "pyproject.toml").write_text(_PACKAGED_PYPROJECT)
    (app_dir / "app.toml").write_text(_PACKAGED_MANIFEST)
    (app_dir / "icon.svg").write_text(_ICON)
    (app_dir / "src" / "fixture_app" / "__init__.py").write_text(_PACKAGED_SERVER)
    worktree = tmp_path / "worktree"
    shutil.copytree(repo_root, worktree)
    worktree_server = (
        worktree / "system" / "apps" / "fixture" / "src" / "fixture_app" / "__init__.py"
    )
    worktree_server.write_text(
        _PACKAGED_SERVER.replace('BUILD = "live build"', 'BUILD = "worktree build"')
    )

    live_env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    subprocess.run(
        ["uv", "sync", "--all-packages"], cwd=repo_root, env=live_env, check=True
    )
    live_bin = repo_root / ".venv" / "bin"
    assert (live_bin / "fixture-serve").exists()
    monkeypatch.setenv("VIRTUAL_ENV", str(repo_root / ".venv"))
    monkeypatch.setenv("PATH", f"{live_bin}{os.pathsep}{os.environ['PATH']}")

    repo_args = ["--repo-root", str(repo_root)]
    assert (
        mod.main(["up", "--app", "fixture", "--worktree", str(worktree), *repo_args])
        == 0
    )
    try:
        url = mod.live_preview_url(repo_root, "fixture")
        assert url is not None
        served = _get(url)
    finally:
        assert mod.main(["down", "--app", "fixture", *repo_args]) == 0

    assert served["build"] == "worktree build"
    served_path = [
        Path(entry).resolve()
        for entry in str(served["path"]).split(os.pathsep)
        if entry
    ]
    assert live_bin.resolve() not in served_path
