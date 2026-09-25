import json
import subprocess
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Final

from imbue.imbue_common.mutable_model import MutableModel
from pydantic import Field
from pydantic import PrivateAttr

APP_ICON_MARKUP: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'
)

# A git command against a freshly made temp repository is instant; anything near this is a hang.
_GIT_TIMEOUT_SECONDS: Final[float] = 30.0

# The app the scope and CLI tests build a workspace around.
NEWS_MANIFEST: Final[str] = """
name = "news"
display_name = "News"
icon = "icon.svg"

[[references]]
path = ".agents/skills/news-refresh"
note = "Fetches stories on a schedule; calls POST /api/ingest"

[[references]]
path = "system/scripts/run_news.sh"

[scope]
exclude = ["docs/generated/**", "data/**"]
"""


def write_app_manifest(repo_root: Path, package: str, body: str, is_icon_written: bool) -> Path:
    """Create ``<repo_root>/system/apps/<package>/app.toml`` (and its icon) with ``body``."""
    app_directory = repo_root / "system" / "apps" / package
    app_directory.mkdir(parents=True, exist_ok=True)
    if is_icon_written:
        (app_directory / "icon.svg").write_text(APP_ICON_MARKUP, encoding="utf-8")
    manifest_path = app_directory / "app.toml"
    manifest_path.write_text(body, encoding="utf-8")
    return manifest_path


def write_supervisord_conf(repo_root: Path, section_names: Sequence[str]) -> Path:
    """Create a ``system/supervisord.conf`` holding one empty block per named section."""
    conf_path = repo_root / "system" / "supervisord.conf"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    blocks = "".join(f"[{section_name}]\ncommand=/bin/true\n\n" for section_name in section_names)
    conf_path.write_text(blocks, encoding="utf-8")
    return conf_path


def write_supervisord_dropin(
    repo_root: Path, file_stem: str, section_names: Sequence[str]
) -> Path:
    """Create a ``system/supervisord.conf.d/<stem>.conf`` holding one empty block per section."""
    conf_path = repo_root / "system" / "supervisord.conf.d" / f"{file_stem}.conf"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    blocks = "".join(f"[{section_name}]\ncommand=/bin/true\n\n" for section_name in section_names)
    conf_path.write_text(blocks, encoding="utf-8")
    return conf_path


def write_repo_file(repo_root: Path, relative_path: str, content: str) -> Path:
    """Create a file at a repo-relative path, making the directories above it."""
    file_path = repo_root / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return file_path


def run_git(repo_root: Path, arguments: Sequence[str]) -> str:
    """Run a git command in ``repo_root``, raising CalledProcessError on failure."""
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    return completed.stdout


def init_git_repository(repo_root: Path) -> None:
    """Make ``repo_root`` a git repository with a committer identity, on a branch named main."""
    repo_root.mkdir(parents=True, exist_ok=True)
    run_git(repo_root, ("init", "-q", "--initial-branch=main"))
    run_git(repo_root, ("config", "user.email", "app-manifest-test@example.invalid"))
    run_git(repo_root, ("config", "user.name", "app manifest test"))


def commit_everything(repo_root: Path, message: str) -> str:
    """Stage and commit the whole tree, returning the new commit's full sha."""
    run_git(repo_root, ("add", "-A"))
    run_git(repo_root, ("commit", "-q", "-m", message))
    return run_git(repo_root, ("rev-parse", "HEAD")).strip()


def build_news_workspace(repo_root: Path) -> Path:
    """A repo-shaped tree holding the news app, everything it references, and a supervisord conf."""
    manifest_path = write_app_manifest(repo_root, "news", NEWS_MANIFEST, is_icon_written=True)
    write_repo_file(repo_root, "system/apps/news/runner.py", "ROUTES = ('/api/ingest',)\n")
    write_repo_file(repo_root, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")
    write_repo_file(repo_root, "system/scripts/run_news.sh", "#!/bin/sh\nexit 0\n")
    write_supervisord_conf(repo_root, ("program:news", "program:news-fetcher", "program:files"))
    return manifest_path


_SELECTION_ROOT_PYPROJECT: Final[str] = """
[project]
name = "workspace"
version = "0.1.0"

[tool.uv.workspace]
members = ["system/libs/*", "system/apps/*"]
exclude = ["system/libs/ui"]

[tool.pytest.ini_options]
addopts = ["--ignore=system/apps/chat"]
"""

_SELECTION_OVERRIDES: Final[str] = """
always_run = ["system/scripts/hook_wiring_test.py"]

[[consumer]]
paths = ["catalog/**"]
suites = ["system/apps/notes"]
note = "reads the catalog"

[[integration]]
test = "system/scripts/test_create_gate.py"
paths = [".mngr/settings.toml"]
note = "runs the real mngr create"
"""

_NOTES_MANIFEST: Final[str] = """
name = "notes"
display_name = "Notes"
icon = "icon.svg"

[[references]]
path = "system/scripts/run_notes.sh"
"""


def _python_package(repo_root: Path, directory: str, name: str, dependencies: Sequence[str]) -> None:
    listed = ", ".join(f'"{dependency}"' for dependency in dependencies)
    module = name.replace("-", "_")
    write_repo_file(
        repo_root,
        f"{directory}/pyproject.toml",
        f'[project]\nname = "{name}"\ndependencies = [{listed}]\n\n'
        f'[tool.hatch.build.targets.wheel]\npackages = ["src/{module}"]\n',
    )
    write_repo_file(repo_root, f"{directory}/src/{module}/__init__.py", "")
    write_repo_file(repo_root, f"{directory}/src/{module}/core.py", "VALUE = 1\n")
    write_repo_file(repo_root, f"{directory}/src/{module}/core_test.py", "def test_value() -> None:\n    pass\n")


def build_selection_workspace(repo_root: Path) -> None:
    """A committed repo shaped like the workspace, for the test selection: a shared library
    (``corelib``) that another library (``midlib``) and the chat app depend on, an app
    (``notes``) that depends on ``midlib`` and references a script, the chat app as its own
    pytest root with a browser test and a frontend, the shared ``ui`` npm library that
    frontend depends on, flat scripts with paired and unpaired tests, a skill whose script
    imports ``corelib``, the repo guards, and an override file."""
    init_git_repository(repo_root)
    write_repo_file(repo_root, "pyproject.toml", _SELECTION_ROOT_PYPROJECT)
    write_repo_file(repo_root, "conftest.py", "")
    write_repo_file(repo_root, "README.md", "# workspace\n")
    write_repo_file(repo_root, "docs/guide.md", "# guide\n")

    # Python packages
    _python_package(repo_root, "system/libs/corelib", "corelib", ())
    _python_package(repo_root, "system/libs/midlib", "midlib", ("corelib>=0.1",))
    _python_package(repo_root, "system/apps/notes", "notes", ("midlib",))
    write_app_manifest(repo_root, "notes", _NOTES_MANIFEST, is_icon_written=True)
    write_repo_file(repo_root, "system/scripts/run_notes.sh", "#!/bin/sh\n")

    # The chat app: its own pytest root, with a browser test and a type-check ratchet
    write_repo_file(
        repo_root,
        "system/apps/chat/pyproject.toml",
        '[project]\nname = "chat"\ndependencies = ["corelib"]\n\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["imbue"]\n\n'
        '[tool.pytest.ini_options]\naddopts = ["-m", "not release"]\n',
    )
    write_repo_file(repo_root, "system/apps/chat/imbue/chat/__init__.py", "")
    write_repo_file(repo_root, "system/apps/chat/imbue/chat/server.py", "PORT = 1\n")
    write_repo_file(repo_root, "system/apps/chat/imbue/chat/server_test.py", "def test_port() -> None:\n    pass\n")
    write_repo_file(
        repo_root,
        "system/apps/chat/imbue/chat/test_e2e.py",
        "from playwright.sync_api import Page\n\n\ndef test_page(page: Page) -> None:\n    pass\n",
    )
    write_repo_file(repo_root, "system/apps/chat/imbue/chat/test_ratchets.py", "def test_no_type_errors() -> None:\n    pass\n")

    # The npm workspace
    write_repo_file(
        repo_root,
        "system/package.json",
        json.dumps({"name": "frontends", "workspaces": ["libs/ui", "apps/chat/frontend"]}),
    )
    write_repo_file(
        repo_root,
        "system/libs/ui/package.json",
        json.dumps(
            {
                "name": "@workspace/ui",
                "scripts": {"test": "vitest run", "lint": "eslint src/", "format:check": "prettier --check src", "typecheck": "tsc --noEmit"},
            }
        ),
    )
    write_repo_file(repo_root, "system/libs/ui/src/index.ts", "export const x = 1;\n")
    write_repo_file(
        repo_root,
        "system/apps/chat/frontend/package.json",
        json.dumps(
            {
                "name": "chat-frontend",
                "dependencies": {"@workspace/ui": "0.1.0"},
                "scripts": {"build": "tsc --noEmit && vite build", "test": "vitest run", "lint": "eslint src/", "format:check": "prettier --check src", "typecheck": "tsc --noEmit"},
            }
        ),
    )
    write_repo_file(repo_root, "system/apps/chat/frontend/src/main.ts", "export {};\n")

    # Flat scripts, a skill, and the repo guards
    write_repo_file(repo_root, "system/scripts/forward_port.py", "PORT = 1\n")
    write_repo_file(repo_root, "system/scripts/forward_port_test.py", "def test_port() -> None:\n    pass\n")
    write_repo_file(repo_root, "system/scripts/agy_shim/agy_shim.sh", "#!/bin/sh\n")
    write_repo_file(repo_root, "system/scripts/agy_shim/agy_shim_test.py", "def test_shim() -> None:\n    pass\n")
    write_repo_file(repo_root, "system/scripts/create_gate.py", "GATE = 1\n")
    write_repo_file(repo_root, "system/scripts/test_create_gate.py", "def test_gate() -> None:\n    pass\n")
    write_repo_file(repo_root, "system/scripts/hook_wiring_test.py", "def test_wiring() -> None:\n    pass\n")
    write_repo_file(
        repo_root,
        "system/scripts/banner_test.py",
        '# Exercises system/scripts/banner.txt.\ndef test_banner() -> None:\n    pass\n',
    )
    write_repo_file(repo_root, "system/scripts/banner.txt", "hello\n")
    write_repo_file(repo_root, "system/test_layout.py", "def test_layout() -> None:\n    pass\n")
    write_repo_file(repo_root, ".agents/skills/refresh/SKILL.md", "# refresh\n")
    write_repo_file(repo_root, ".agents/skills/refresh/scripts/refresh.py", "from corelib.core import VALUE\n")
    write_repo_file(repo_root, ".agents/skills/refresh/scripts/refresh_test.py", "def test_refresh() -> None:\n    pass\n")
    write_repo_file(repo_root, ".mngr/settings.toml", "")
    write_repo_file(repo_root, "catalog/templates.json", "[]\n")
    write_repo_file(repo_root, "system/libs/app_manifest/src/app_manifest/test_selection_overrides.toml", _SELECTION_OVERRIDES)
    commit_everything(repo_root, "workspace")


class ShellStub(MutableModel):
    """A loopback stand-in for the shell that answers every GET with one configured status and body."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid", "frozen": False}

    status: int = Field(default=200, description="The status every request is answered with")
    body: str = Field(default="{}", description="The body every request is answered with")
    _server: ThreadingHTTPServer | None = PrivateAttr(default=None)
    _thread: threading.Thread | None = PrivateAttr(default=None)

    def start(self) -> None:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                encoded = stub.body.encode("utf-8")
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the shell stub is not serving")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def answer(self, status: int, body: str) -> None:
        self.status = status
        self.body = body

    def close(self) -> None:
        """Stop serving; the URL then refuses connections, as a shell that is down does."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
