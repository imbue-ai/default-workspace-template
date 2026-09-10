"""Shared helpers for the app_manifest test files: a repo-shaped tree to load manifests out of."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

APP_ICON_MARKUP = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'


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
        timeout=30,
    )
    return completed.stdout


def init_git_repository(repo_root: Path) -> None:
    """Make ``repo_root`` a git repository with a committer identity, on a branch named main."""
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo_root)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    run_git(repo_root, ("config", "user.email", "app-manifest-test@example.invalid"))
    run_git(repo_root, ("config", "user.name", "app manifest test"))


def commit_everything(repo_root: Path, message: str) -> str:
    """Stage and commit the whole tree, returning the new commit's full sha."""
    run_git(repo_root, ("add", "-A"))
    run_git(repo_root, ("commit", "-q", "-m", message))
    return run_git(repo_root, ("rev-parse", "HEAD")).strip()
