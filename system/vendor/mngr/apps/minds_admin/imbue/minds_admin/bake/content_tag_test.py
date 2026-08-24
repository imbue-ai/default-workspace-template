import subprocess
from pathlib import Path

import pytest

from imbue.minds_admin.bake.content_tag import ContentTagError
from imbue.minds_admin.bake.content_tag import compute_content_addressed_cache_tag


def _make_git_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    (workspace / "Dockerfile").write_text("FROM scratch\n")
    return workspace


def test_content_tag_is_stable_for_identical_content(tmp_path: Path) -> None:
    workspace = _make_git_workspace(tmp_path)

    first_tag = compute_content_addressed_cache_tag(workspace)
    second_tag = compute_content_addressed_cache_tag(workspace)

    assert first_tag == second_tag
    assert first_tag.startswith("default-workspace-template:content-")


def test_content_tag_changes_when_a_file_changes(tmp_path: Path) -> None:
    workspace = _make_git_workspace(tmp_path)
    original_tag = compute_content_addressed_cache_tag(workspace)

    (workspace / "Dockerfile").write_text("FROM scratch\nRUN true\n")

    assert compute_content_addressed_cache_tag(workspace) != original_tag


def test_content_tag_includes_untracked_files(tmp_path: Path) -> None:
    workspace = _make_git_workspace(tmp_path)
    original_tag = compute_content_addressed_cache_tag(workspace)

    vendor_dir = workspace / "system" / "vendor" / "mngr"
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "main.py").write_text("print('vendored')\n")

    assert compute_content_addressed_cache_tag(workspace) != original_tag


def test_content_tag_ignores_gitignored_files(tmp_path: Path) -> None:
    workspace = _make_git_workspace(tmp_path)
    (workspace / ".gitignore").write_text("*.log\n")
    original_tag = compute_content_addressed_cache_tag(workspace)

    (workspace / "scratch.log").write_text("noise that must not affect the tag\n")

    assert compute_content_addressed_cache_tag(workspace) == original_tag


def test_content_tag_does_not_touch_the_real_index(tmp_path: Path) -> None:
    workspace = _make_git_workspace(tmp_path)

    compute_content_addressed_cache_tag(workspace)

    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    # The Dockerfile must still be untracked (??), not staged (A) -- staging
    # through the throwaway index must not leak into the repo's own index.
    assert status.stdout.startswith("?? "), status.stdout


def test_content_tag_rejects_a_non_git_directory(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()

    with pytest.raises(ContentTagError, match="not a git work tree"):
        compute_content_addressed_cache_tag(plain_dir)
