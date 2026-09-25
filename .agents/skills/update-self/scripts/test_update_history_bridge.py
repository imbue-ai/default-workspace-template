"""Tests for ``update_self.py bridge-history`` against a template rewritten the way upstream's is."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import update_self
from update_history_bridge import DEFAULT_STATE_PATH, REWRITTEN_PATHS

# The rewrite runs git-filter-repo through uvx, which downloads it on a cold cache.
pytestmark = pytest.mark.acceptance

VENDORED_FILE = "system/vendor/mngr/libs/mngr/cli.py"

# The filter upstream's rewrite runs.
_UPSTREAM_FILTER = (
    "uvx",
    "--from",
    "git-filter-repo==2.47.0",
    "git-filter-repo",
    "--force",
    "--preserve-commit-hashes",
    "--invert-paths",
    *(arg for path in REWRITTEN_PATHS for arg in ("--path", path)),
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "advice.detachedHead=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str | None]) -> None:
    for rel, content in files.items():
        path = repo / rel
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def template(tmp_path: Path) -> Path:
    """A template that vendors mngr, released as ``minds-v1`` and ``minds-v1.1``."""
    template = tmp_path / "template"
    template.mkdir()
    _git(template, "init", "-q", "-b", "main")
    _commit(
        template,
        "Start the template",
        {".gitignore": "data/\n", "README.md": "one\n", VENDORED_FILE: "v1\n"},
    )
    _commit(template, "Improve the readme", {"README.md": "two\n"})
    _commit(template, "Refresh vendored mngr", {VENDORED_FILE: "v2\n"})
    _git(template, "tag", "-a", "minds-v1", "-m", "minds-v1")
    _commit(template, "Document the template", {"docs.md": "docs\n"})
    _git(template, "tag", "-a", "minds-v1.1", "-m", "minds-v1.1")
    return template


@pytest.fixture
def upstream(tmp_path: Path, template: Path) -> Path:
    """``template`` rewritten as upstream's is, then released again as ``minds-v2`` without the vendored copy."""
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "clone", "-q", "--mirror", template.as_uri(), str(upstream))
    subprocess.run(_UPSTREAM_FILTER, cwd=upstream, capture_output=True, check=True)
    work = tmp_path / "release"
    _git(tmp_path, "clone", "-q", str(upstream), str(work))
    _commit(work, "Pin mngr by git", {"pyproject.toml": "mngr = 'git'\n"})
    _git(work, "tag", "-a", "minds-v2", "-m", "minds-v2")
    _git(work, "push", "-q", "origin", "main", "minds-v2")
    return upstream


def _workspace(
    tmp_path: Path,
    template: Path,
    upstream: Path,
    *,
    depth: int | None = None,
    message: str = "Add my notes",
    files: dict[str, str | None] | None = None,
) -> Path:
    """A workspace created from ``minds-v1`` before the rewrite, with a commit of its own."""
    workspace = tmp_path / "workspace"
    depth_args = ["--depth", str(depth)] if depth else []
    _git(
        tmp_path,
        "clone",
        "-q",
        *depth_args,
        "--branch",
        "minds-v1",
        template.as_uri(),
        str(workspace),
    )
    _git(workspace, "checkout", "-q", "-B", "main")
    _commit(workspace, message, {"notes.md": "mine\n"} if files is None else files)
    _git(workspace, "remote", "add", "upstream", str(upstream))
    _git(workspace, "fetch", "-q", "upstream", "--tags", "--force")
    return workspace


@pytest.fixture
def workspace(tmp_path: Path, template: Path, upstream: Path) -> Path:
    return _workspace(tmp_path, template, upstream)


def _bridge(workspace: Path, capsys) -> dict:
    assert (
        update_self.main(
            ["bridge-history", "--ref", "minds-v2", "--repo-root", str(workspace)]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


def _drop(workspace: Path, capsys) -> dict:
    assert (
        update_self.main(["bridge-history", "--drop", "--repo-root", str(workspace)])
        == 0
    )
    return json.loads(capsys.readouterr().out)


def _merge_release(workspace: Path) -> None:
    """Merge ``minds-v2`` as the worker does: always a merge commit, never a fast-forward."""
    _git(workspace, "merge", "-q", "--no-ff", "--no-edit", "minds-v2")


def _replace_refs(repo: Path) -> list[str]:
    return _git(repo, "replace", "-l").splitlines()


def test_bridge_history_is_a_no_op_for_a_workspace_on_the_rewritten_history(
    tmp_path, upstream, capsys
) -> None:
    workspace = tmp_path / "fresh"
    _git(tmp_path, "clone", "-q", "--branch", "minds-v1", str(upstream), str(workspace))

    result = _bridge(workspace, capsys)
    dropped = _drop(workspace, capsys)

    assert result["bridged"] is False
    assert result["fork_point"] == _git(upstream, "rev-parse", "minds-v1^{commit}")
    assert dropped["dropped"] is None
    assert _replace_refs(workspace) == []
    assert not (workspace / "data").exists()


def test_bridge_history_makes_the_old_fork_point_the_merge_base(
    template, upstream, workspace, capsys
) -> None:
    old_fork = _git(template, "rev-parse", "minds-v1^{commit}")
    old_head = _git(workspace, "rev-parse", "HEAD")
    assert (
        subprocess.run(
            ["git", "merge-base", "HEAD", "minds-v2"], cwd=workspace
        ).returncode
        == 1
    )

    result = _bridge(workspace, capsys)

    assert result["bridged"] is True
    assert result["fork_point"] == old_fork
    assert _git(workspace, "merge-base", "HEAD", "minds-v2") == old_fork
    # The vendor-only release commit is gone upstream, so its twin is the commit before it.
    assert result["twin"] == _git(upstream, "rev-parse", "minds-v1^{commit}")
    assert _git(workspace, "rev-parse", "HEAD") == old_head


def test_a_bridged_merge_lands_the_release_and_keeps_local_work(
    workspace, capsys
) -> None:
    old_head = _git(workspace, "rev-parse", "HEAD")
    _bridge(workspace, capsys)

    _merge_release(workspace)

    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", old_head, "HEAD"], cwd=workspace
        ).returncode
        == 0
    )
    assert not (workspace / VENDORED_FILE).exists()
    assert (workspace / "notes.md").read_text() == "mine\n"
    assert (workspace / "pyproject.toml").read_text() == "mngr = 'git'\n"
    assert _git(workspace, "diff", "--name-only", "minds-v2", "HEAD").splitlines() == [
        "notes.md"
    ]


def test_bridge_history_forks_at_the_newest_release_the_workspace_merged(
    template, upstream, workspace, capsys
) -> None:
    # An update before the rewrite took the workspace to minds-v1.1; upstream has since moved the tag.
    _git(
        workspace,
        "fetch",
        "-q",
        "origin",
        "refs/tags/minds-v1.1:refs/pre-rewrite/minds-v1.1",
    )
    _git(
        workspace,
        "merge",
        "-q",
        "--no-edit",
        "-m",
        "update-self: merge upstream template (minds-v1.1)",
        "refs/pre-rewrite/minds-v1.1",
    )

    result = _bridge(workspace, capsys)

    assert result["fork_point"] == _git(template, "rev-parse", "minds-v1.1^{commit}")
    assert result["twin"] == _git(upstream, "rev-parse", "minds-v1.1^{commit}")
    _merge_release(workspace)
    assert _git(workspace, "diff", "--name-only", "minds-v2", "HEAD").splitlines() == [
        "notes.md"
    ]


def test_bridge_history_drops_the_graft_once_the_merge_has_landed(
    workspace, capsys
) -> None:
    twin = _bridge(workspace, capsys)["twin"]
    _merge_release(workspace)

    result = _bridge(workspace, capsys)

    assert result["bridged"] is False
    assert result["dropped"] == twin
    assert _replace_refs(workspace) == []
    assert not (workspace / DEFAULT_STATE_PATH).exists()
    assert _git(workspace, "merge-base", "HEAD", "minds-v2") == _git(
        workspace, "rev-parse", "minds-v2^{commit}"
    )


def test_bridge_history_is_repeatable_before_the_merge(workspace, capsys) -> None:
    first = _bridge(workspace, capsys)

    second = _bridge(workspace, capsys)

    assert second == first
    assert len(_replace_refs(workspace)) == 1


def test_bridge_history_replaces_a_graft_an_earlier_pass_left(
    workspace, capsys
) -> None:
    stale = _git(workspace, "rev-parse", "minds-v2^{commit}")
    unrelated = _git(workspace, "commit-tree", "-m", "Unrelated", "HEAD^{tree}")
    _git(
        workspace,
        "replace",
        "--graft",
        stale,
        *_git(workspace, "rev-parse", f"{stale}^@").split(),
        unrelated,
    )
    state = workspace / DEFAULT_STATE_PATH
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"twin": stale, "fork_point": unrelated}))

    result = _bridge(workspace, capsys)

    assert result["dropped"] == stale
    assert _replace_refs(workspace) == [result["twin"]]
    _drop(workspace, capsys)
    assert _replace_refs(workspace) == []


def test_bridge_history_bridges_a_shallow_workspace(
    tmp_path, template, upstream, capsys
) -> None:
    workspace = _workspace(tmp_path, template, upstream, depth=1)
    boundary = (workspace / ".git/shallow").read_text().split()[0]

    result = _bridge(workspace, capsys)

    assert result["fork_point"] == boundary
    _merge_release(workspace)
    assert not (workspace / VENDORED_FILE).exists()


def test_a_worker_worktree_sees_the_bridge(tmp_path, workspace, capsys) -> None:
    worker = tmp_path / "worker"
    _git(
        workspace,
        "worktree",
        "add",
        "-q",
        "-b",
        "mngr/update-self",
        str(worker),
        "HEAD",
    )

    result = _bridge(workspace, capsys)

    assert _git(worker, "merge-base", "HEAD", "minds-v2") == result["fork_point"]


def test_bridge_history_refuses_a_workspace_it_cannot_match_and_changes_nothing(
    tmp_path, upstream, capsys
) -> None:
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    _git(stranger, "init", "-q", "-b", "main")
    _commit(stranger, "Unrelated", {"README.md": "other\n"})
    _git(
        stranger, "fetch", "-q", str(upstream), "refs/tags/minds-v2:refs/tags/minds-v2"
    )

    assert (
        update_self.main(
            ["bridge-history", "--ref", "minds-v2", "--repo-root", str(stranger)]
        )
        == 1
    )

    assert "shares no history" in capsys.readouterr().err
    assert _replace_refs(stranger) == []
    assert not (stranger / "data").exists()


def test_bridge_history_drop_removes_a_live_graft(workspace, capsys) -> None:
    twin = _bridge(workspace, capsys)["twin"]

    result = _drop(workspace, capsys)

    assert result["dropped"] == twin
    assert _replace_refs(workspace) == []
    assert list((workspace / DEFAULT_STATE_PATH).parent.iterdir()) == []
    assert (
        subprocess.run(
            ["git", "merge-base", "HEAD", "minds-v2"], cwd=workspace
        ).returncode
        == 1
    )


def test_bridge_history_drop_forgets_a_record_whose_graft_is_gone(
    workspace, capsys
) -> None:
    twin = _bridge(workspace, capsys)["twin"]
    _git(workspace, "replace", "-d", twin)

    result = _drop(workspace, capsys)

    assert result["dropped"] == twin
    assert list((workspace / DEFAULT_STATE_PATH).parent.iterdir()) == []


def test_a_descendant_with_the_fork_tree_is_the_merge_base(
    tmp_path, template, upstream, capsys
) -> None:
    # The release's vendor refresh undone: HEAD's tree is the twin's plus the older vendored copy.
    workspace = _workspace(
        tmp_path,
        template,
        upstream,
        message="Put the vendored copy back",
        files={VENDORED_FILE: "v1\n"},
    )

    old_head = _git(workspace, "rev-parse", "HEAD")

    result = _bridge(workspace, capsys)

    assert result["fork_point"] == old_head
    _merge_release(workspace)
    assert _git(workspace, "rev-parse", "HEAD^1") == old_head
    assert _git(workspace, "diff", "--name-only", "minds-v2", "HEAD") == ""
