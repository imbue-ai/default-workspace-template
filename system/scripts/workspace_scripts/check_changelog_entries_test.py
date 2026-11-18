"""Tests for the per-project changelog gate.

The gate maps changed files to the projects that own them and fails when a
touched project is missing its per-PR entry file. The most important behaviors
to lock down are: (1) it refuses to pass vacuously when the only resolvable
diff base is HEAD itself (the sandbox / shallow-clone footgun), and (2) it maps
files to projects and detects missing entries correctly against a real git
repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workspace_scripts import check_changelog_entries as gate


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


def test_project_for_path_maps_libs_services_apps_and_dev(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    assert gate.project_for_path("system/libs/alpha/foo.py", repo) == "libs/alpha"
    assert gate.project_for_path("system/apps/beta/bar.py", repo) == "apps/beta"
    assert (
        gate.project_for_path("system/services/gamma/baz.py", repo) == "services/gamma"
    )
    # A system/libs/ dir without a pyproject.toml is not a real project -> dev.
    assert gate.project_for_path("system/libs/nope/x.py", repo) == "dev"
    # Anything under .agents/ -> the synthetic agents bucket.
    assert gate.project_for_path(".agents/skills/foo/SKILL.md", repo) == "agents"
    assert gate.project_for_path(".agents/shared/references/x.md", repo) == "agents"
    # Root-level files -> dev.
    assert gate.project_for_path("system/scripts/thing.sh", repo) == "dev"
    assert gate.project_for_path("README.md", repo) == "dev"


def test_gate_flags_agents_change_missing_entry_and_clears_with_one(
    tmp_path: Path,
) -> None:
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


def test_gate_fails_when_touched_project_missing_entry(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "system/libs/alpha/new.py").write_text("print(1)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch alpha")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"libs/alpha"}
    assert gate.find_missing_entries("feat/x", touched, repo) == [
        "system/libs/alpha/changelog/feat-x.md"
    ]


def test_a_library_and_an_app_of_the_same_name_each_owe_their_own_entry(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    (repo / "system/apps/alpha").mkdir(parents=True)
    (repo / "system/apps/alpha/pyproject.toml").write_text("[project]\nname='x'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "an app named like the library")
    _git(repo, "checkout", "-q", "-b", "feat/z")
    (repo / "system/libs/alpha/new.py").write_text("print(1)\n")
    (repo / "system/apps/alpha/new.py").write_text("print(2)\n")
    _add_entry(repo, "system/libs/alpha", "feat/z")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch both")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"libs/alpha", "apps/alpha"}
    # The library's entry does not stand in for the app's.
    assert gate.find_missing_entries("feat/z", touched, repo) == [
        "system/apps/alpha/changelog/feat-z.md"
    ]


def test_gate_passes_when_entry_present(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/y")
    (repo / "system/apps/beta/new.py").write_text("print(1)\n")
    _add_entry(repo, "system/apps/beta", "feat/y")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch beta with entry")

    base = gate.resolve_diff_base(repo)
    changed = gate.changed_files_against_base(base, repo)
    touched = gate.projects_requiring_entry(changed, repo)
    assert touched == {"apps/beta"}
    assert gate.find_missing_entries("feat/y", touched, repo) == []


def test_resolve_diff_base_refuses_head_collision(tmp_path: Path) -> None:
    """When main == HEAD (e.g. a fresh clone with no distinct base), the gate
    must raise rather than diff against HEAD and pass vacuously."""
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
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "stacked-base")
    (repo / "README.md").write_text("changed\n")
    _git(repo, "commit", "-qam", "move HEAD off the base")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("CHANGELOG_BASE_REF", "stacked-base")

    assert gate.resolve_diff_base(repo) == "stacked-base"


_UNSTAMPED_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n- Added: A feature.\n"
_STAMPED_CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [minds-v1.1.0] - 2026-05-11\n\n- Added: A feature.\n"


def _branch_changing_the_root_changelog(
    tmp_path: Path, branch: str, before: str | None, after: str
) -> Path:
    """A repo whose ``branch`` touches a library and takes the root ``CHANGELOG.md``
    from ``before`` (on main; ``None`` for no file) to ``after``."""
    repo = _init_repo(tmp_path)
    if before is not None:
        (repo / gate.ROOT_CHANGELOG).write_text(before)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "changelog")
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / gate.ROOT_CHANGELOG).write_text(after)
    (repo / "system/libs/alpha/new.py").write_text("print(1)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change the changelog and a library")
    return repo


def test_a_consolidation_branch_owes_no_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "mngr/changelog-consolidation-2026-11-17")
    (repo / "system/libs/alpha/new.py").write_text("print(1)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch alpha with no entry")

    assert gate.main(repo) == 0
    assert "is exempt" in capsys.readouterr().out


def test_only_a_consolidation_branch_is_exempt_by_name() -> None:
    assert gate.is_exempt_branch("mngr/changelog-consolidation-catch-up")
    assert not gate.is_exempt_branch("mngr/some-feature")
    assert not gate.is_exempt_branch("fork/mngr/changelog-consolidation")


def test_a_pull_request_that_adds_a_release_section_owes_no_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _branch_changing_the_root_changelog(
        tmp_path, "release", _UNSTAMPED_CHANGELOG, _STAMPED_CHANGELOG
    )

    assert gate.main(repo) == 0
    assert "adds a release section" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(None, _UNSTAMPED_CHANGELOG, id="new-file-with-only-unreleased"),
        pytest.param(
            _UNSTAMPED_CHANGELOG,
            _UNSTAMPED_CHANGELOG.replace("A feature.", "A better feature."),
            id="unreleased-bullet",
        ),
        pytest.param(
            _STAMPED_CHANGELOG,
            _STAMPED_CHANGELOG.replace("A feature.", "A better feature."),
            id="released-bullet",
        ),
    ],
)
def test_a_changelog_change_that_adds_no_release_section_still_owes_entries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    before: str | None,
    after: str,
) -> None:
    repo = _branch_changing_the_root_changelog(tmp_path, "correction", before, after)

    assert gate.main(repo) == 1
    err = capsys.readouterr().err
    assert "system/changelog/correction.md" in err
    assert "system/libs/alpha/changelog/correction.md" in err
