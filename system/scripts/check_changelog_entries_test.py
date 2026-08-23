"""Tests for the per-project changelog gate.

The gate maps changed files to the projects that own them and fails when a
touched project is missing its per-PR entry file. The most important behaviors
to lock down are: (1) it refuses to pass vacuously when the only resolvable
diff base is HEAD itself (the sandbox / shallow-clone footgun), and (2) it maps
files to projects and detects missing entries correctly against a real git
repo.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "check_changelog_entries.py"
_spec = importlib.util.spec_from_file_location("check_changelog_entries", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal monorepo-shaped git repo with a `main` branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    # Two real projects (pyproject.toml present) plus the dev bucket layout.
    for rel in (
        "system/libs/alpha/pyproject.toml",
        "system/apps/beta/pyproject.toml",
        "system/services/gamma/pyproject.toml",
    ):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[project]\nname='x'\n")
    (repo / "README.md").write_text("root\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _add_entry(repo: Path, project_dir: str, branch: str) -> None:
    d = repo / project_dir / "changelog"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{branch.replace('/', '-')}.md").write_text("did a thing\n")


def _isolate_base_ref_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the runner's own base-ref env from a sandbox-repo test.

    ``resolve_diff_base`` honors ``CHANGELOG_BASE_REF`` / ``GITHUB_BASE_REF``
    from the environment, and on a stacked PR the CI runner's
    ``GITHUB_BASE_REF`` names a branch that exists in the real checkout but
    not in these tests' sandbox repos -- which turned every sandbox resolve
    into the refuse-to-fall-back error. Tests that SET these vars to exercise
    that path do so explicitly on top of this clean slate.
    """
    monkeypatch.delenv("CHANGELOG_BASE_REF", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)


def test_project_for_path_maps_libs_services_apps_and_dev(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert gate.project_for_path("system/libs/alpha/foo.py", repo) == "alpha"
    assert gate.project_for_path("system/apps/beta/bar.py", repo) == "beta"
    assert gate.project_for_path("system/services/gamma/baz.py", repo) == "gamma"
    # A system/libs/ dir without a pyproject.toml is not a real project -> dev.
    assert gate.project_for_path("system/libs/nope/x.py", repo) == "dev"
    # Anything under .agents/ -> the synthetic agents bucket.
    assert gate.project_for_path(".agents/skills/foo/SKILL.md", repo) == "agents"
    assert gate.project_for_path(".agents/shared/references/x.md", repo) == "agents"
    # Root-level files -> dev.
    assert gate.project_for_path("system/scripts/thing.sh", repo) == "dev"
    assert gate.project_for_path("README.md", repo) == "dev"


def test_gate_flags_agents_change_missing_entry_and_clears_with_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_base_ref_env(monkeypatch)
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/skill")
    skill = repo / ".agents/skills/foo/SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# a skill\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch a skill")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"agents"}
    assert gate.find_missing_entries("feat/skill", touched, repo) == [
        ".agents/changelog/feat-skill.md"
    ]

    _add_entry(repo, ".agents", "feat/skill")
    assert gate.find_missing_entries("feat/skill", touched, repo) == []


def test_gate_fails_when_touched_project_missing_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_base_ref_env(monkeypatch)
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "system/libs/alpha/new.py").write_text("print(1)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch alpha")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"alpha"}
    assert gate.find_missing_entries("feat/x", touched, repo) == [
        "system/libs/alpha/changelog/feat-x.md"
    ]


def test_gate_passes_when_entry_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_base_ref_env(monkeypatch)
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/y")
    (repo / "system/apps/beta/new.py").write_text("print(1)\n")
    _add_entry(repo, "system/apps/beta", "feat/y")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch beta with entry")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"beta"}
    assert gate.find_missing_entries("feat/y", touched, repo) == []


def test_resolve_diff_base_refuses_head_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When main == HEAD (e.g. a fresh clone with no distinct base), the gate
    must raise rather than diff against HEAD and pass vacuously."""
    _isolate_base_ref_env(monkeypatch)
    repo = _init_repo(tmp_path)
    # Still on main, so main resolves to HEAD; no other base ref exists.
    with pytest.raises(RuntimeError):
        gate.resolve_diff_base(repo)


def test_resolve_diff_base_refuses_an_unresolvable_named_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named base that does not resolve must raise, never fall back to main.

    On a stacked PR the real base is several commits above main, so falling
    back diffs the whole stack and demands entries the PR does not owe. It
    presented as flakiness -- the same files green on one run and red on the
    next -- because the answer was confidently wrong rather than absent.
    """
    _isolate_base_ref_env(monkeypatch)
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("changed\n")
    _git(repo, "commit", "-qam", "move HEAD off the base")
    monkeypatch.setenv("GITHUB_BASE_REF", "preston/no-such-branch")

    with pytest.raises(RuntimeError) as excinfo:
        gate.resolve_diff_base(repo)

    assert "preston/no-such-branch" in str(excinfo.value)
    assert "main" in str(excinfo.value)


def test_resolve_diff_base_uses_a_named_base_that_does_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_base_ref_env(monkeypatch)
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "stacked-base")
    (repo / "README.md").write_text("changed\n")
    _git(repo, "commit", "-qam", "move HEAD off the base")
    monkeypatch.setenv("GITHUB_BASE_REF", "stacked-base")

    assert gate.resolve_diff_base(repo) == "stacked-base"


def test_changelog_base_ref_overrides_the_reserved_github_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow cannot fix GITHUB_BASE_REF, so it must be overridable.

    GitHub reserves the `GITHUB_` prefix: a job-level `env: GITHUB_BASE_REF:`
    is echoed in the log and then ignored, leaving the runner's value in place.
    On a stacked PR that value was `main` instead of the parent branch, so the
    only way to pass the payload's answer through is a name GitHub does not own.
    """
    _isolate_base_ref_env(monkeypatch)
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "stacked-base")
    (repo / "README.md").write_text("changed\n")
    _git(repo, "commit", "-qam", "move HEAD off the base")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("CHANGELOG_BASE_REF", "stacked-base")

    assert gate.resolve_diff_base(repo) == "stacked-base"
