"""Shared test helpers for the github_sync test files."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from app_manifest.records import (
    AppOrigin,
    AppRecord,
    app_directory_prefix,
    write_record,
)
from imbue.imbue_common.model_update import to_update

from github_sync.app_history import AppSplit, open_app_split
from github_sync.visibility import VISIBILITY_PRIVATE


def run_git(repo: Path, *args: str) -> None:
    """Run a git command in `repo`, raising on failure."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_repo(repo: Path) -> None:
    """Create a git repo at `repo` with a committer identity configured."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    run_git(repo, "config", "user.email", "test@test.local")
    run_git(repo, "config", "user.name", "test")


def init_repo_with_origin(base: Path) -> tuple[Path, Path]:
    """Create a seeded main repo at base/main with a bare origin at base/origin.git."""
    main = base / "main"
    init_repo(main)
    (main / "seed.txt").write_text("seed\n")
    run_git(main, "add", "-A")
    run_git(main, "commit", "-qm", "seed")
    origin = base / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True
    )
    run_git(main, "remote", "add", "origin", str(origin))
    return main, origin


def install_fake_latchkey(bin_dir: Path, script_body: str) -> None:
    """Install an executable `latchkey` shell script into `bin_dir`.

    The caller prepends `bin_dir` to PATH (via monkeypatch.setenv) so
    check_repo_visibility exercises its real subprocess path against a
    controllable stand-in instead of the real latchkey CLI.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "latchkey"
    script.write_text(f"#!/usr/bin/env bash\n{script_body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def make_git_exec_path_whose_split_never_ends(directory: Path) -> Path:
    """A git command directory (for `GIT_EXEC_PATH`) whose `git subtree` only
    waits, as a split does that has more history to read than it is given time
    for."""
    real_exec_path = Path(
        subprocess.run(
            ["git", "--exec-path"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    directory.mkdir(parents=True)
    for command in real_exec_path.iterdir():
        if command.name != "git-subtree":
            os.symlink(command, directory / command.name)
    stand_in = directory / "git-subtree"
    stand_in.write_text("#!/bin/sh\nexec sleep 3\n")
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


def add_history(repo: Path, branch: str, count: int) -> None:
    """Add `count` commits to `branch` of `repo`, each changing one file of the
    workspace and none of any app, without checking anything out: a long
    history, made in a moment, of commits dated before anything a test commits.
    """
    tip = git_output(
        repo, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}"
    )
    already = int(git_output(repo, "rev-list", "--count", tip)) if tip else 0
    stream: list[str] = []
    for number in range(already + 1, already + count + 1):
        message = f"Template commit {number}\n"
        text = f"release {number}\n"
        stream.append(
            f"commit refs/heads/{branch}\n"
            f"committer test <test@test.local> {1_600_000_000 + number * 60} +0000\n"
            f"data {len(message)}\n{message}"
        )
        if tip and number == already + 1:
            stream.append(f"from {tip}\n")
        stream.append(f"M 100644 inline RELEASE.md\ndata {len(text)}\n{text}\n")
    subprocess.run(
        ["git", "-C", str(repo), "fast-import", "--quiet"],
        input="".join(stream),
        text=True,
        check=True,
        capture_output=True,
    )


def git_output(repo: Path, *args: str) -> str:
    """Run a git command in `repo` and return its stdout, stripped."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def commit_all(repo: Path, message: str) -> str:
    """Commit everything in `repo` and return the commit."""
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", message)
    return git_output(repo, "rev-parse", "HEAD")


def init_workspace(path: Path) -> Path:
    """A git repo standing in for a workspace: one commit, its data untracked."""
    init_repo(path)
    (path / "README.md").write_text("a workspace\n")
    (path / ".gitignore").write_text("data/\n")
    commit_all(path, "Initial workspace commit")
    return path


def init_bare_repo(path: Path) -> Path:
    """An empty bare repo standing in for an app's dev repository."""
    subprocess.run(
        ["git", "init", "-q", "--bare", "--initial-branch=main", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def write_app_file(workspace: Path, record: AppRecord, name: str, text: str) -> None:
    """Write one file of the app's directory, uncommitted."""
    path = workspace / app_directory_prefix(record) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def add_owned_app(workspace: Path, name: str, dev_repo_url: str = "") -> AppRecord:
    """Commit an app this workspace built, with its record, and return the record."""
    record = AppRecord(
        name=name,
        package=name.replace("-", "_"),
        port=8080,
        origin=AppOrigin.LOCAL,
        dev_repo_url=dev_repo_url,
    )
    write_app_file(workspace, record, "app.toml", f'name = "{name}"\n')
    write_record(workspace, record)
    commit_all(workspace, f"Add the {name} app")
    return record


def link_app(workspace: Path, record: AppRecord) -> str:
    """Make the app's first split with a join marker, as `app-install link`
    does, and return the split commit."""
    split = git_output(
        workspace,
        "subtree",
        "split",
        "--quiet",
        f"--prefix={app_directory_prefix(record)}",
        "--rejoin",
    )
    return split.splitlines()[-1]


def split_commit(workspace: Path, record: AppRecord) -> str:
    """The commit of the app's own history the workspace's checked-out commit
    splits to, as the sync pass makes it. Its objects are gone again with the
    scratch clone, so this is for comparing one split with another."""
    with open_app_split(workspace, record) as split:
        return split.commit


def split_output(split: AppSplit, *args: str) -> str:
    """Run a git command in the scratch clone of an open split, reading the
    history as it is rather than as the clone was cut for the split."""
    return git_output(split.clone, "--no-replace-objects", *args)


def commit_on_branch(
    repo: Path, branch: str, name: str, text: str, message: str
) -> str:
    """Commit one file on another branch of `repo` and come back to `main`."""
    run_git(repo, "checkout", "-q", branch)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    commit = commit_all(repo, message)
    run_git(repo, "checkout", "-q", "main")
    return commit


def init_published_app(path: Path, name: str) -> Path:
    """A repo holding one published app at its root, with two commits."""
    init_repo(path)
    (path / "app.toml").write_text(f'name = "{name}"\n')
    commit_all(path, f"{name}: first version")
    (path / "uv.lock").write_text("the publisher's lock\n")
    (path / "notes.py").write_text("GREETING = 'hello'\n")
    commit_all(path, f"{name}: second version")
    return path


def install_app(workspace: Path, published: Path, name: str) -> AppRecord:
    """Add a published app to the workspace as `app-install install` does (a
    squashed subtree and a record) and return the record."""
    package = name.replace("-", "_")
    commit = git_output(published, "rev-parse", "HEAD")
    run_git(
        workspace,
        "subtree",
        "add",
        f"--prefix=system/apps/{package}",
        str(published),
        "main",
        "--squash",
    )
    record = AppRecord(
        name=name,
        package=package,
        port=8081,
        origin=AppOrigin.INSTALLED,
        upstream_url=str(published),
        upstream_commit=commit,
        upstream_tree=git_output(published, "rev-parse", f"{commit}^{{tree}}"),
    )
    write_record(workspace, record)
    commit_all(workspace, f"Register {name}")
    return record


def update_installed_app(
    workspace: Path, published: Path, record: AppRecord
) -> AppRecord:
    """Bring the published app's newest commit into the workspace over its own
    changes, as `app-install update` does, and return the record."""
    commit = git_output(published, "rev-parse", "HEAD")
    run_git(
        workspace,
        "subtree",
        "pull",
        f"--prefix={app_directory_prefix(record)}",
        str(published),
        "main",
        "--squash",
        "-m",
        f"Update {record.name}",
    )
    updated = record.model_copy_update(
        to_update(record.field_ref().upstream_commit, commit),
        to_update(
            record.field_ref().upstream_tree,
            git_output(published, "rev-parse", f"{commit}^{{tree}}"),
        ),
    )
    write_record(workspace, updated)
    commit_all(workspace, f"Record the update of {record.name}")
    return updated


def always_private(repo_url: str) -> str:
    """A visibility check for dev repositories that are all private."""
    return VISIBILITY_PRIVATE
