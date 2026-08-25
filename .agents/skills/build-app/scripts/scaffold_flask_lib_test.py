"""Tests for the build-app scaffolder's icon handling.

Driven end to end as a subprocess against a minimal fake repo root, the way
the skill invokes it (with ``--skip-uv-sync`` so no real environment is
materialized).
"""

import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parent / "scaffold_flask_lib.py"

_ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2"><path d="M4 4h16v16H4z"/></svg>'
)

_ROOT_PYPROJECT = """\
[project]
name = "fake-root"
version = "0.1.0"
dependencies = []

[tool.uv]

[tool.uv.workspace]
members = ["system/apps/*"]

[tool.uv.sources]
"""


def _make_repo_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "system").mkdir(parents=True)
    (repo_root / "pyproject.toml").write_text(_ROOT_PYPROJECT)
    (repo_root / "system" / "supervisord.conf").write_text("[supervisord]\n")
    return repo_root


def _run(repo_root: Path, extra_args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--repo-root",
            str(repo_root),
            "--skip-uv-sync",
            *extra_args,
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_scaffold_ships_the_icon_and_registers_it_on_every_start(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    icon_file = tmp_path / "draft-icon.svg"
    icon_file.write_text(f"\n{_ICON}\n")

    result = _run(
        repo_root,
        ["--name", "my-app", "--description", "a test app", "--icon-file", str(icon_file)],
    )

    assert result.returncode == 0, result.stderr
    # The icon is copied into the lib (stripped), so the lib owns it and a
    # later edit + restart updates the registered markup.
    assert (repo_root / "system" / "apps" / "my_app" / "icon.svg").read_text() == _ICON + "\n"
    # The generated supervisord command registers that file on every start.
    conf = (repo_root / "system" / "supervisord.conf").read_text()
    assert "--icon-file system/apps/my_app/icon.svg" in conf


def test_scaffold_refuses_a_missing_icon_file(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)

    result = _run(
        repo_root,
        [
            "--name",
            "my-app",
            "--description",
            "a test app",
            "--icon-file",
            str(tmp_path / "nope.svg"),
        ],
    )

    assert result.returncode != 0
    assert "does not exist" in result.stderr
    assert not (repo_root / "system" / "apps" / "my_app").exists()


def test_scaffold_refuses_invalid_icon_markup(tmp_path: Path) -> None:
    """The scaffold validates with forward_port.py's own rules, so a bad icon
    fails here instead of on the app's first supervisord start."""
    repo_root = _make_repo_root(tmp_path)
    icon_file = tmp_path / "bad.svg"
    icon_file.write_text("<svg><script>alert(1)</script></svg>")

    result = _run(
        repo_root,
        ["--name", "my-app", "--description", "a test app", "--icon-file", str(icon_file)],
    )

    assert result.returncode != 0
    assert "invalid icon" in result.stderr
    assert not (repo_root / "system" / "apps" / "my_app").exists()


def test_scaffold_requires_an_icon_file(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)

    result = _run(repo_root, ["--name", "my-app", "--description", "a test app"])

    assert result.returncode != 0
    assert "--icon-file" in result.stderr
