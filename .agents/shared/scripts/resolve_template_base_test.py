"""Tests for ``resolve_template_base.py``.

The base decides what a published template contains and what history it ships,
so the script tests build the commit shapes bootstrap and update-self actually
write and assert on the content the resolved base carries. The
``find_template_base`` tests cover log shapes that are awkward to build in git.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).with_name("resolve_template_base.py")
_spec = importlib.util.spec_from_file_location("resolve_template_base", _SCRIPT)
assert _spec is not None and _spec.loader is not None
resolve_template_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_template_base)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, relative: str, message: str) -> str:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{message}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_workspace(root: Path) -> tuple[Path, str, str]:
    """A template commit with bootstrap's marker on top; returns (repo, template, initial)."""
    repo = root / "workspace"
    repo.mkdir()
    _git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    template = _commit(repo, "pyproject.toml", "Template release one")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Initial workspace commit")
    return repo, template, _git(repo, "rev-parse", "HEAD")


def _resolve(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
    )


def test_an_updated_workspace_resolves_to_the_upstream_template_it_merged(
    tmp_path: Path,
) -> None:
    repo, template, _ = _new_workspace(tmp_path)
    pre_update = _commit(repo, "system/apps/music_scout/main.py", "Build music scout")
    _git(repo, "checkout", "-q", "-b", "upstream", template)
    upstream = _commit(repo, "system/release_two.py", "Template release two")
    _git(repo, "checkout", "-q", "main")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "upstream",
        "-m",
        "update-self: merge upstream template (minds-v0.0.2)",
    )
    _commit(repo, "system/apps/demo/main.py", "Build the app being published")

    completed = _resolve(repo)

    assert completed.returncode == 0, completed.stderr
    base = completed.stdout.strip()
    assert base == upstream
    # What a publish on this base would ship: the new template release, and
    # neither the app built before the update nor the commit that built it.
    tree = _git(repo, "ls-tree", "-r", "--name-only", base).splitlines()
    assert "system/release_two.py" in tree
    assert "system/apps/music_scout/main.py" not in tree
    is_ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", pre_update, base]
    )
    assert is_ancestor.returncode == 1


def test_an_update_self_subject_that_merged_nothing_is_not_a_marker(
    tmp_path: Path,
) -> None:
    repo, _, initial = _new_workspace(tmp_path)
    _commit(repo, ".agents/skills/update-self/SKILL.md", "update-self: tidy the skill")

    completed = _resolve(repo)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == initial


def test_a_history_without_markers_exits_nonzero_for_the_callers_fallback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "handmade"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    _commit(repo, "pyproject.toml", "Initial commit")

    completed = _resolve(repo)

    assert completed.returncode == 1
    assert completed.stdout == ""
    # Both questions fall back the same way, so the caller handles one case.
    assert _resolve(repo, "--origin").returncode == 1


def test_the_origin_is_this_workspaces_own_marker_not_an_ancestors(
    tmp_path: Path,
) -> None:
    """The template repo is itself developed from workspaces.

    A full-history clone therefore reaches bootstrap markers that belong to
    somebody else's workspace, and dating this one by those reports a stranger's
    creation.
    """
    repo, _, ancestor_marker = _new_workspace(tmp_path)
    _commit(repo, "system/release_two.py", "Template release two")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "Initial workspace commit")
    own_marker = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "system/apps/demo/main.py", "Build the app")

    completed = _resolve(repo, "--origin")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == own_marker
    assert own_marker != ancestor_marker


def test_an_update_self_merge_does_not_move_the_origin(tmp_path: Path) -> None:
    """Where the mind started never changes; only the base it is on does."""
    repo, template, initial = _new_workspace(tmp_path)
    _git(repo, "checkout", "-q", "-b", "upstream", template)
    upstream = _commit(repo, "system/release_two.py", "Template release two")
    _git(repo, "checkout", "-q", "main")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "upstream",
        "-m",
        "update-self: merge upstream template (minds-v0.0.2)",
    )

    assert _resolve(repo, "--origin").stdout.strip() == initial
    assert _resolve(repo).stdout.strip() == upstream


def test_find_template_base_takes_the_newest_marker() -> None:
    log = [
        "aaa1111\tbbb2222\tAdd the email triage app",
        "bbb2222\tccc3333 up09999\tupdate-self: merge upstream template (minds-v0.3.9)",
        "ccc3333\tddd4444\tTweak the welcome skill",
        "ddd4444\teee5555 up06666\tupdate-self: merge upstream template (minds-v0.3.6)",
        "eee5555\tfff6666\tInitial workspace commit",
    ]
    # update-self's origin-line walk takes the OLDEST marker instead.
    assert resolve_template_base.find_template_base(log) == "up09999"


def test_find_template_base_returns_none_without_a_marker() -> None:
    assert resolve_template_base.find_template_base([]) is None
    assert (
        resolve_template_base.find_template_base(
            ["aaa1111\t\tInitial commit", "", "  "]
        )
        is None
    )


def test_find_template_base_ignores_a_marker_that_is_not_the_subject_prefix() -> None:
    # A commit merely *mentioning* update-self is not a template-state marker;
    # only the `update-self:` subject prefix is.
    log = ["aaa1111\tbbb2222\tFix the update-self skill's conflict triage"]
    assert resolve_template_base.find_template_base(log) is None


def test_find_template_base_reads_past_an_empty_subject_commit() -> None:
    # `git commit --allow-empty-message` leaves nothing after the last tab.
    log = ["aaa1111\tbbb2222\t", "bbb2222\tccc3333\tInitial workspace commit"]
    assert resolve_template_base.find_template_base(log) == "bbb2222"
