"""`ledger.py`: the record of published apps."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from publish_to_github_pages_skill.pages_testing import git, run_tool


def _ledger(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_tool("ledger.py", *args, cwd=root)


def _record(
    root: Path, url: str = "https://octo.github.io/demo/"
) -> subprocess.CompletedProcess[str]:
    return _ledger(
        root,
        "record",
        "--app",
        "demo",
        "--owner",
        "octo",
        "--repo",
        "demo",
        "--url",
        url,
        "--published-commit",
        "abc",
        "--data",
        "two prices",
    )


def _workspace(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(
        tmp_path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "w",
    )
    return tmp_path


def test_an_app_never_published_has_no_entry(tmp_path: Path) -> None:
    result = _ledger(tmp_path, "get", "--app", "demo")
    assert result.returncode == 1
    assert "has not been published" in result.stderr


def test_a_recorded_site_is_read_back_with_the_workspace_commit(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    assert _record(root).returncode == 0
    entry = json.loads(_ledger(root, "get", "--app", "demo").stdout)
    assert entry["url"] == "https://octo.github.io/demo/"
    assert entry["data"] == "two prices"
    assert entry["workspace_commit"] == git(root, "rev-parse", "HEAD")
    assert entry["published_at"].endswith("+00:00")


def test_a_republish_replaces_the_entry_and_removal_forgets_it(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    _record(root)
    _record(root, url="https://octo.github.io/demo-2/")
    entries = json.loads(_ledger(root, "list").stdout)
    assert list(entries) == ["demo"]
    assert entries["demo"]["url"] == "https://octo.github.io/demo-2/"
    assert _ledger(root, "remove", "--app", "demo").returncode == 0
    assert json.loads(_ledger(root, "list").stdout) == {}
    ledger_dir = root / "data" / ".skills" / "publish-to-github-pages"
    assert sorted(p.name for p in ledger_dir.iterdir()) == ["sites.json"]
