import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

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
