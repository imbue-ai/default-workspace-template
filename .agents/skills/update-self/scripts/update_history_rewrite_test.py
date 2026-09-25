"""Tests for ``update_self.py rewrite-history`` against a template rewritten the way upstream's was."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import update_self
from update_history_rewrite import FILTER_REPO_ARGS, FILTER_REPO_COMMAND

VENDORED_FILE = "system/vendor/mngr/libs/mngr/cli.py"


def _git(repo: Path, *args: str, check: bool = True) -> str:
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
        check=check,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _template(tmp_path: Path) -> Path:
    """A template that vendors mngr, released as ``minds-v1``."""
    template = tmp_path / "template"
    template.mkdir()
    _git(template, "init", "-q", "-b", "main")
    _commit(
        template,
        "Start the template",
        {".gitignore": "data/\n", "README.md": "one\n", VENDORED_FILE: "v1\n"},
    )
    # A commit only upstream carries (a pull request's head), named in a later message.
    _git(template, "checkout", "-q", "-b", "proposal")
    _commit(template, "Propose a readme", {"README.md": "proposal\n"})
    proposal = _git(template, "rev-parse", "HEAD")
    _git(template, "update-ref", "refs/pull/1/head", proposal)
    _git(template, "checkout", "-q", "main")
    _git(template, "branch", "-q", "-D", "proposal")
    _commit(
        template,
        f"Improve the readme (supersedes {proposal[:10]})",
        {"README.md": "two\n"},
    )
    _commit(template, "Refresh vendored mngr", {VENDORED_FILE: "v2\n"})
    _git(template, "tag", "-a", "minds-v1", "-m", "minds-v1")
    return template


def _rewritten_upstream(tmp_path: Path, template: Path) -> Path:
    """``template`` rewritten as upstream's was, then released again as ``minds-v2``."""
    upstream = tmp_path / "upstream.git"
    _git(tmp_path, "clone", "-q", "--mirror", template.as_uri(), str(upstream))
    subprocess.run(
        [*FILTER_REPO_COMMAND, *FILTER_REPO_ARGS],
        cwd=upstream,
        capture_output=True,
        check=True,
    )
    work = tmp_path / "release"
    _git(tmp_path, "clone", "-q", str(upstream), str(work))
    _commit(work, "Pin mngr by git", {"pyproject.toml": "mngr = 'git'\n"})
    _git(work, "tag", "-a", "minds-v2", "-m", "minds-v2")
    _git(work, "push", "-q", "origin", "main", "minds-v2")
    return upstream


def _workspace(
    tmp_path: Path, template: Path, upstream: Path, *, depth: int | None = None
) -> Path:
    """A workspace created from ``minds-v1`` before the rewrite, with its own commits."""
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
    _commit(workspace, "Add my notes", {"notes.md": "mine\n"})
    _commit(
        workspace,
        "Tweak the readme and vendored mngr",
        {"README.md": "mine\n", VENDORED_FILE: "mine\n"},
    )
    _git(workspace, "remote", "add", "upstream", str(upstream))
    # The old flow's fetch: moved tags are refused, the new release still lands.
    _git(workspace, "fetch", "-q", "upstream", "--tags", check=False)
    return workspace


def _rewrite(workspace: Path, capsys) -> dict:
    assert (
        update_self.main(
            ["rewrite-history", "--ref", "minds-v2", "--repo-root", str(workspace)]
        )
        == 0
    )
    return json.loads(capsys.readouterr().out)


def _rewritten_v1(upstream: Path) -> str:
    return _git(upstream, "rev-parse", "minds-v1^{commit}")


def test_rewrite_history_gives_an_old_workspace_the_rewritten_fork_point(
    tmp_path, capsys
) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    workspace = _workspace(tmp_path, template, upstream)
    assert (
        subprocess.run(
            ["git", "merge-base", "HEAD", "minds-v2"], cwd=workspace
        ).returncode
        == 1
    )

    result = _rewrite(workspace, capsys)

    assert result["rewritten"] is True
    assert result["fork_point"] == _rewritten_v1(upstream)
    assert _git(workspace, "merge-base", "HEAD", "minds-v2") == _rewritten_v1(upstream)
    assert _git(workspace, "rev-parse", "minds-v1") == _git(
        upstream, "rev-parse", "minds-v1"
    )
    assert _git(workspace, "log", "--format=%s", "-2").splitlines() == [
        "Tweak the readme and vendored mngr",
        "Add my notes",
    ]
    assert _git(workspace, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "README.md"
    ]
    reachable = _git(
        workspace, "rev-list", "--objects", "--branches", "--tags"
    ).splitlines()
    assert not [line for line in reachable if " system/vendor" in line]
    assert _git(workspace, "status", "--porcelain") == ""
    assert (workspace / VENDORED_FILE).read_text() == "mine\n"
    assert not list(
        (workspace / "data/.tasks/update-self/history-rewrite").glob("*.json")
    )
    assert _git(workspace, "for-each-ref", "refs/update-self") == ""


def test_rewrite_history_makes_the_next_tag_fetch_clean(tmp_path, capsys) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    workspace = _workspace(tmp_path, template, upstream)

    _rewrite(workspace, capsys)

    fetch = subprocess.run(
        ["git", "fetch", "upstream", "--tags"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    assert fetch.returncode == 0, fetch.stderr


def test_rewrite_history_is_a_no_op_once_history_is_shared(tmp_path, capsys) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    workspace = _workspace(tmp_path, template, upstream)
    _rewrite(workspace, capsys)
    head = _git(workspace, "rev-parse", "HEAD")

    result = _rewrite(workspace, capsys)

    assert result == {
        "rewritten": False,
        "fork_point": _rewritten_v1(upstream),
        "moved_refs": 0,
    }
    assert _git(workspace, "rev-parse", "HEAD") == head


def test_rewrite_history_carries_a_shallow_workspace_onto_the_rewritten_release(
    tmp_path, capsys
) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    workspace = _workspace(tmp_path, template, upstream, depth=1)
    assert (workspace / ".git/shallow").exists()

    result = _rewrite(workspace, capsys)

    assert result["rewritten"] is True
    assert _git(workspace, "rev-parse", "HEAD~2") == _rewritten_v1(upstream)
    assert _git(workspace, "merge-base", "HEAD", "minds-v2") == _rewritten_v1(upstream)
    assert _git(workspace, "status", "--porcelain") == ""


def test_rewrite_history_keeps_a_linked_worktree_clean(tmp_path, capsys) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    workspace = _workspace(tmp_path, template, upstream)
    linked = tmp_path / "linked"
    _git(workspace, "worktree", "add", "-q", "-b", "side", str(linked), "HEAD~1")

    result = _rewrite(workspace, capsys)

    assert result["rewritten"] is True
    assert _git(linked, "status", "--porcelain") == ""
    assert _git(linked, "merge-base", "HEAD", "minds-v2") == _rewritten_v1(upstream)


def test_rewrite_history_refuses_uncommitted_changes_and_changes_nothing(
    tmp_path, capsys
) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    workspace = _workspace(tmp_path, template, upstream)
    (workspace / "notes.md").write_text("unsaved\n")
    head = _git(workspace, "rev-parse", "HEAD")

    assert (
        update_self.main(
            ["rewrite-history", "--ref", "minds-v2", "--repo-root", str(workspace)]
        )
        == 1
    )

    assert "uncommitted changes" in capsys.readouterr().err
    assert _git(workspace, "rev-parse", "HEAD") == head


def test_rewrite_history_refuses_a_workspace_the_rewrite_cannot_connect(
    tmp_path, capsys
) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    _git(stranger, "init", "-q", "-b", "main")
    _commit(stranger, "Unrelated", {"README.md": "other\n"})
    _git(
        stranger, "fetch", "-q", str(upstream), "refs/tags/minds-v2:refs/tags/minds-v2"
    )
    head = _git(stranger, "rev-parse", "HEAD")

    assert (
        update_self.main(
            ["rewrite-history", "--ref", "minds-v2", "--repo-root", str(stranger)]
        )
        == 1
    )

    assert "shares no history" in capsys.readouterr().err
    assert _git(stranger, "rev-parse", "HEAD") == head
    assert _git(stranger, "for-each-ref", "refs/update-self") == ""


def test_rewrite_history_finishes_a_run_interrupted_after_moving_refs(
    tmp_path, capsys
) -> None:
    template = _template(tmp_path)
    upstream = _rewritten_upstream(tmp_path, template)
    workspace = _workspace(tmp_path, template, upstream)
    old_head = _git(workspace, "rev-parse", "HEAD")
    _rewrite(workspace, capsys)
    new_head = _git(workspace, "rev-parse", "HEAD")
    # Refs moved, the index not yet: what a kill between the two leaves behind.
    _git(workspace, "read-tree", old_head)
    scratch = workspace / "data/.tasks/update-self/history-rewrite"
    (scratch / "journal.json").write_text(
        json.dumps(
            {
                "head": new_head,
                "refs": ["refs/heads/main"],
                "worktrees": [
                    {"path": str(workspace), "old": old_head, "new": new_head}
                ],
            }
        )
    )
    assert _git(workspace, "status", "--porcelain") != ""

    result = _rewrite(workspace, capsys)

    assert result["rewritten"] is True
    assert _git(workspace, "status", "--porcelain") == ""
    assert not (scratch / "journal.json").exists()
