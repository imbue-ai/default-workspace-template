"""Test helpers: a throwaway workspace tree with user apps in it."""

import os
import subprocess
from pathlib import Path

from app_install.git import COMMITTER_EMAIL
from app_install.git import COMMITTER_NAME
from app_install.git import run_git
from app_manifest.testing import APP_ICON_MARKUP
from app_manifest.testing import write_app_manifest

MINIMAL_PYPROJECT = """\
[project]
name = "{name}"
version = "0.1.0"

[project.scripts]
{name} = "{package}.runner:main"
"""


def manifest_body(name: str, extra: str = "") -> str:
    """A user app's manifest: the scaffold's required fields, then ``extra``."""
    return f'name = "{name}"\ndisplay_name = "{name.title()}"\nicon = "icon.svg"\n{extra}'


def write_user_app(repo_root: Path, name: str, extra_manifest: str = "", is_python: bool = True) -> str:
    """Create ``system/apps/<package>`` for the app ``name`` and return the package directory's name."""
    package = name.replace("-", "_")
    write_app_manifest(repo_root, package, manifest_body(name, extra_manifest), is_icon_written=True)
    if is_python:
        (repo_root / "system" / "apps" / package / "pyproject.toml").write_text(
            MINIMAL_PYPROJECT.format(name=name, package=package)
        )
    return package


def write_app_file(repo_root: Path, package: str, relative_path: str, text: str = "") -> Path:
    """Create a file inside the app's directory."""
    path = repo_root / "system" / "apps" / package / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_workspace_file(repo_root: Path, relative_path: str, text: str = "") -> Path:
    """Create a file elsewhere in the workspace tree."""
    path = repo_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def never_bound(port: int) -> bool:
    """A port check for a tree nothing listens in."""
    return False


def commit_all(repository: Path, message: str) -> str:
    """Commit everything in ``repository`` and return the commit."""
    run_git(repository, ["add", "--all"])
    run_git(repository, ["commit", "--quiet", "--allow-empty", "-m", message])
    return run_git(repository, ["rev-parse", "HEAD"])


def commit_all_dated(repository: Path, message: str, date: str) -> str:
    """Commit everything in ``repository`` as if on ``date`` (an ISO 8601 time) and return the commit."""
    run_git(repository, ["add", "--all"])
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": COMMITTER_NAME,
        "GIT_AUTHOR_EMAIL": COMMITTER_EMAIL,
        "GIT_COMMITTER_NAME": COMMITTER_NAME,
        "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "commit", "--quiet", "--allow-empty", "-m", message],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
    )
    return run_git(repository, ["rev-parse", "HEAD"])


def make_workspace_repository(directory: Path) -> Path:
    """A git repository standing in for a workspace: one commit, nothing installed."""
    directory.mkdir(parents=True, exist_ok=True)
    run_git(directory, ["init", "--quiet", "--initial-branch=main"])
    (directory / "README.md").write_text("a workspace\n")
    # The workspace's data, the installer's lock included, is never tracked.
    (directory / ".gitignore").write_text("data/\n")
    commit_all(directory, "Initial workspace commit")
    return directory


def make_published_app_repository(directory: Path, name: str, extra_manifest: str = "") -> Path:
    """A git repository holding one published app at its root, as an adopter's installer finds it."""
    directory.mkdir(parents=True, exist_ok=True)
    run_git(directory, ["init", "--quiet", "--initial-branch=main"])
    (directory / "app.toml").write_text(manifest_body(name, extra_manifest))
    (directory / "icon.svg").write_text(APP_ICON_MARKUP)
    (directory / "README.md").write_text(f"# {name}\n")
    commit_all(directory, f"{name}: first version")
    return directory


def make_bare_repository(directory: Path) -> Path:
    """An empty bare repository, standing in for one on GitHub."""
    directory.parent.mkdir(parents=True, exist_ok=True)
    run_git(directory.parent, ["init", "--quiet", "--bare", "--initial-branch=main", str(directory)])
    return directory


def point_url_at(workspace: Path, url: str, repository: Path) -> None:
    """Have every git command run in ``workspace`` reach ``repository`` when it names ``url``, so no test reaches
    GitHub for a repository the installer's stand-in runtime says it created there."""
    run_git(workspace, ["config", f"url.{repository}.insteadOf", url])
