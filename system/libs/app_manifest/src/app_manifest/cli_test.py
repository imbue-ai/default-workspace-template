import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from click.testing import Result

from app_manifest.cli import app_manifest_cli
from app_manifest.testing import APP_ICON_MARKUP
from app_manifest.testing import build_news_workspace
from app_manifest.testing import build_selection_workspace
from app_manifest.testing import commit_changes
from app_manifest.testing import commit_everything
from app_manifest.testing import declare_root_dependency
from app_manifest.testing import init_git_repository
from app_manifest.testing import run_git
from app_manifest.testing import selection_lock
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_repo_file
from app_manifest.testing import write_supervisord_dropin


def _run_cli(arguments: list[str]) -> Result:
    return CliRunner().invoke(app_manifest_cli, arguments)


def test_validate_manifest_accepts_a_valid_manifest(tmp_path: Path) -> None:
    (tmp_path / "icon.svg").write_text(APP_ICON_MARKUP)
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n')

    result = _run_cli(["validate-manifest", str(manifest_path)])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "ok: news (News)"


def test_validate_manifest_reports_the_failing_field_and_exits_non_zero(tmp_path: Path) -> None:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\nbogus = 1\n')

    result = _run_cli(["validate-manifest", str(manifest_path)])

    assert result.exit_code != 0
    assert "bogus" in result.output


def test_validate_manifest_checks_the_references_only_when_given_a_repo_root(
    tmp_path: Path,
) -> None:
    # Off the system/apps/<package>/app.toml layout there is no root to derive, so the
    # location checks run only when the caller names one.
    loose_directory = tmp_path / "scaffold"
    loose_directory.mkdir()
    (loose_directory / "icon.svg").write_text(APP_ICON_MARKUP)
    manifest_path = loose_directory / "app.toml"
    manifest_path.write_text(
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[[references]]\npath = "docs/system/news.md"\n'
    )

    without_root = _run_cli(["validate-manifest", str(manifest_path)])
    with_root = _run_cli(
        ["validate-manifest", str(manifest_path), "--repo-root", str(tmp_path)]
    )

    assert without_root.exit_code == 0, without_root.output
    assert with_root.exit_code != 0
    assert "docs/system/news.md" in with_root.output


def test_footprint_writes_the_scope_file_for_an_app_to_stdout(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(
        ["footprint", "system/apps/news/app.toml", "--repo-root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    scope = json.loads(result.output)
    assert scope["creation"] == {
        "type": "app",
        "name": "news",
        "package": "news",
        "manifest": "system/apps/news/app.toml",
    }
    assert scope["primary"] == ["system/apps/news/"]
    assert scope["wiring"] == [
        {
            "path": "system/supervisord.conf",
            "sections": ["program:news", "program:news-fetcher"],
        }
    ]
    assert scope["references"] == [
        {
            "path": ".agents/skills/news-refresh",
            "note": "Fetches stories on a schedule; calls POST /api/ingest",
            "kind": "skill",
        },
        {"path": "system/scripts/run_news.sh", "note": None, "kind": "script"},
    ]
    assert scope["context"] == []
    assert scope["exclude"][-1] == "docs/generated/**"
    assert scope["diff"] is None


def test_footprint_writes_the_scope_file_to_out_making_the_directories_above_it(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)
    out_path = tmp_path / "data" / ".tasks" / "harden" / "scope.json"

    result = _run_cli(
        [
            "footprint",
            "system/apps/news/app.toml",
            "--repo-root",
            str(tmp_path),
            "--out",
            str(out_path),
        ]
    )

    assert result.exit_code == 0, result.output
    written = out_path.read_text()
    assert written.endswith("}\n")
    assert json.loads(written)["creation"]["name"] == "news"


def test_footprint_for_a_path_describes_a_skill_and_names_the_app_that_owns_it(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(
        [
            "footprint",
            "--for-path",
            ".agents/skills/news-refresh",
            "--repo-root",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    scope = json.loads(result.output)
    assert scope["creation"] == {
        "type": "skill",
        "name": "news-refresh",
        "package": None,
        "manifest": None,
    }
    assert scope["primary"] == [".agents/skills/news-refresh/"]
    assert scope["wiring"] == []
    assert scope["references"] == []
    assert scope["context"] == ["system/apps/news/"]
    assert scope["conventions"][0] == ".agents/shared/worker/references/type-skill.md"


def test_footprint_refuses_both_a_manifest_and_a_path(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(
        [
            "footprint",
            "system/apps/news/app.toml",
            "--for-path",
            ".agents/skills/news-refresh",
            "--repo-root",
            str(tmp_path),
        ]
    )

    assert result.exit_code != 0
    assert "not both" in result.output


def test_footprint_refuses_neither_a_manifest_nor_a_path(tmp_path: Path) -> None:
    result = _run_cli(["footprint", "--repo-root", str(tmp_path)])

    assert result.exit_code != 0
    assert "--for-path" in result.output


def test_footprint_reports_a_manifest_it_cannot_load(tmp_path: Path) -> None:
    write_app_manifest(tmp_path, "news", 'name = "news"\ndisplay_name = "News"\n', is_icon_written=False)

    result = _run_cli(
        ["footprint", "system/apps/news/app.toml", "--repo-root", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "icon is required" in result.output


def test_footprint_reports_a_diff_base_git_cannot_resolve(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    init_git_repository(tmp_path)
    commit_everything(tmp_path, "base")

    result = _run_cli(
        [
            "footprint",
            "system/apps/news/app.toml",
            "--repo-root",
            str(tmp_path),
            "--diff-base",
            "no-such-ref-41a7de",
        ]
    )

    assert result.exit_code != 0
    assert "rev-parse" in result.output


def test_footprint_with_a_diff_base_separates_the_changes_outside_the_footprint(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)
    write_repo_file(tmp_path, "docs/system/unrelated.md", "# unrelated\n")
    init_git_repository(tmp_path)
    base_sha = commit_everything(tmp_path, "base")
    write_repo_file(tmp_path, "system/apps/news/runner.py", "ROUTES = ('/api/entries',)\n")
    write_repo_file(tmp_path, "docs/system/unrelated.md", "# unrelated, edited\n")
    commit_everything(tmp_path, "the change under review")

    result = _run_cli(
        [
            "footprint",
            "system/apps/news/app.toml",
            "--repo-root",
            str(tmp_path),
            "--diff-base",
            base_sha,
        ]
    )

    assert result.exit_code == 0, result.output
    diff = json.loads(result.output)["diff"]
    assert diff["base"] == base_sha
    assert sorted(diff["files"]) == [
        "docs/system/unrelated.md",
        "system/apps/news/runner.py",
    ]
    assert diff["inside_footprint"] == ["system/apps/news/runner.py"]
    assert diff["outside_footprint"] == ["docs/system/unrelated.md"]


def test_footprint_diffs_to_the_ref_named_by_diff_ref_instead_of_head(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    init_git_repository(tmp_path)
    base_sha = commit_everything(tmp_path, "base")
    write_repo_file(tmp_path, "system/apps/news/runner.py", "ROUTES = ('/api/entries',)\n")
    middle_sha = commit_everything(tmp_path, "the workspace's own change")
    write_repo_file(tmp_path, "system/scripts/run_news.sh", "#!/bin/sh\nexit 1\n")
    commit_everything(tmp_path, "a later change HEAD carries")

    result = _run_cli(
        [
            "footprint",
            "system/apps/news/app.toml",
            "--repo-root",
            str(tmp_path),
            "--diff-base",
            base_sha,
            "--diff-ref",
            middle_sha,
        ]
    )

    assert result.exit_code == 0, result.output
    diff = json.loads(result.output)["diff"]
    assert diff["ref"] == middle_sha
    assert diff["files"] == ["system/apps/news/runner.py"]


def test_footprint_refuses_a_diff_ref_without_a_diff_base(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(
        [
            "footprint",
            "system/apps/news/app.toml",
            "--repo-root",
            str(tmp_path),
            "--diff-ref",
            "HEAD",
        ]
    )

    assert result.exit_code != 0
    assert "--diff-base" in result.output


def test_references_prints_one_json_object_per_app_that_owns_the_path(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(
        [
            "references",
            "--for-path",
            ".agents/skills/news-refresh/scripts/run.py",
            "--repo-root",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert lines == [
        {
            "app": "news",
            "manifest": "system/apps/news/app.toml",
            "path": ".agents/skills/news-refresh",
            "note": "Fetches stories on a schedule; calls POST /api/ingest",
        }
    ]


def test_references_prints_nothing_and_exits_zero_when_no_app_owns_the_path(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(
        ["references", "--for-path", ".agents/skills/unowned", "--repo-root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_references_still_answers_when_another_apps_manifest_cannot_load(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    write_app_manifest(tmp_path, "broken", 'name = "broken"\n', is_icon_written=False)

    result = _run_cli(
        [
            "references",
            "--for-path",
            ".agents/skills/news-refresh",
            "--repo-root",
            str(tmp_path),
        ]
    )

    assert result.exit_code == 0, result.output
    assert [json.loads(line)["app"] for line in result.output.splitlines() if line.strip()] == [
        "news"
    ]


def _branch_with_changes(repo_root: Path, changes: dict[str, str]) -> None:
    """Put the selection workspace on a branch off main that changes the given files."""
    run_git(repo_root, ("checkout", "-q", "-b", "work"))
    commit_changes(repo_root, changes, "work")


def test_select_tests_over_explicit_paths_matches_the_same_set_read_from_a_diff(
    tmp_path: Path,
) -> None:
    build_selection_workspace(tmp_path)
    base_lock = selection_lock("2.0")
    write_repo_file(tmp_path, "uv.lock", base_lock)
    commit_everything(tmp_path, "lock")
    changes = {
        "system/libs/midlib/src/midlib/core.py": "VALUE = 2\n",
        ".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n",
        "uv.lock": selection_lock("2.1"),
        "README.md": "# changed\n",
    }
    _branch_with_changes(tmp_path, changes)

    from_diff = _run_cli(["select-tests", "--repo-root", str(tmp_path), "--diff-base", "main"])
    path_arguments = [argument for path in changes for argument in ("--path", path)]
    from_paths = _run_cli(
        ["select-tests", "--repo-root", str(tmp_path), "--diff-base", "main", *path_arguments]
    )

    assert from_diff.exit_code == 0, from_diff.output
    assert from_paths.exit_code == 0, from_paths.output
    assert from_paths.output == from_diff.output
    # The upgraded lock entry reached corelib's dependents through the lockfile, not a guess.
    assert "uv run pytest system/libs/corelib" in from_diff.output.splitlines()


def test_select_tests_over_a_diff_runs_what_depended_on_a_moved_files_old_place(
    tmp_path: Path,
) -> None:
    build_selection_workspace(tmp_path)
    run_git(tmp_path, ("checkout", "-q", "-b", "work"))
    run_git(
        tmp_path,
        ("mv", "system/libs/corelib/src/corelib/core.py", "system/libs/midlib/src/midlib/moved.py"),
    )
    commit_everything(tmp_path, "move corelib's core into midlib")

    result = _run_cli(["select-tests", "--repo-root", str(tmp_path), "--diff-base", "main"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    # corelib lost the module its consumers import, so it and they run, not only midlib's side.
    assert "uv run pytest system/libs/corelib" in lines
    assert (
        "(cd system/apps/chat && uv run pytest --deselect imbue/chat/test_ratchets.py::test_no_type_errors)"
        in lines
    )


def test_select_tests_in_a_fresh_worktree_builds_the_frontends_before_an_apps_tests(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    repo_root.mkdir()
    build_selection_workspace(repo_root)
    _branch_with_changes(repo_root, {"system/apps/notes/src/notes/core.py": "VALUE = 2\n"})
    worktree = tmp_path / "worker"
    run_git(repo_root, ("worktree", "add", "-q", "-b", "worker", str(worktree), "work"))

    in_worktree = _run_cli(["select-tests", "--repo-root", str(worktree), "--diff-base", "main"])
    in_built_tree = _run_cli(["select-tests", "--repo-root", str(repo_root), "--diff-base", "main"])

    assert in_worktree.exit_code == 0, in_worktree.output
    assert in_built_tree.exit_code == 0, in_built_tree.output
    build = ["(cd system && npm ci)", "(cd system && npm run build)"]
    # git leaves the ignored built modules out of the worktree, so the notes run there builds them.
    worktree_commands = [line for line in in_worktree.output.splitlines() if not line.startswith("#")]
    assert worktree_commands[:2] == build
    assert "uv run pytest system/apps/notes" in worktree_commands
    assert not set(build) & set(in_built_tree.output.splitlines())


def test_select_tests_over_a_diff_refuses_a_working_tree_with_uncommitted_changes(
    tmp_path: Path,
) -> None:
    build_selection_workspace(tmp_path)
    _branch_with_changes(tmp_path, {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n"})
    write_repo_file(tmp_path, ".git/info/exclude", "*.log\n")
    write_repo_file(tmp_path, "system/libs/midlib/run.log", "ignored\n")
    write_repo_file(tmp_path, "system/libs/midlib/src/midlib/core.py", "VALUE = 3\n")
    write_repo_file(tmp_path, "system/libs/midlib/src/midlib/extra.py", "EXTRA = 1\n")

    dirty = _run_cli(["select-tests", "--repo-root", str(tmp_path), "--diff-base", "main"])
    commit_everything(tmp_path, "the rest of the work")
    committed = _run_cli(["select-tests", "--repo-root", str(tmp_path), "--diff-base", "main"])

    assert dirty.exit_code != 0
    assert "system/libs/midlib/src/midlib/core.py" in dirty.output
    assert "system/libs/midlib/src/midlib/extra.py" in dirty.output
    assert "run.log" not in dirty.output
    assert committed.exit_code == 0, committed.output
    assert "uv run pytest system/libs/midlib" in committed.output.splitlines()


def test_select_tests_over_a_diff_to_an_earlier_commit_ignores_the_working_tree(
    tmp_path: Path,
) -> None:
    build_selection_workspace(tmp_path)
    _branch_with_changes(tmp_path, {"system/scripts/forward_port.py": "PORT = 2\n"})
    earlier_commit = run_git(tmp_path, ("rev-parse", "HEAD")).strip()
    write_repo_file(tmp_path, "system/libs/midlib/src/midlib/core.py", "VALUE = 3\n")
    commit_everything(tmp_path, "a later change")
    write_repo_file(tmp_path, "system/libs/midlib/src/midlib/extra.py", "EXTRA = 1\n")

    result = _run_cli(
        [
            "select-tests",
            "--repo-root",
            str(tmp_path),
            "--diff-base",
            f"{earlier_commit}^",
            "--diff-ref",
            earlier_commit,
        ]
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[-1] == "uv run pytest"
    assert "uv run pytest system/libs/midlib" not in lines


def test_select_tests_prints_json_with_every_path_classified(tmp_path: Path) -> None:
    build_selection_workspace(tmp_path)

    result = _run_cli(
        [
            "select-tests",
            "--repo-root",
            str(tmp_path),
            "--path",
            "system/scripts/forward_port.py",
            "--path",
            "unknown.bin",
            "--format",
            "json",
        ]
    )

    assert result.exit_code == 0, result.output
    selection = json.loads(result.output)
    assert selection["is_full_root"] is True
    assert [command["argv"] for command in selection["commands"]] == [["uv", "run", "pytest"]]
    assert {entry["path"]: entry["classes"] for entry in selection["paths"]} == {
        "system/scripts/forward_port.py": ["unowned"],
        "unknown.bin": ["unowned"],
    }


def _write_pass_scope(
    repo_root: Path, scope_path: Path, creations: list[str], diff_base: str, lead_ref: str | None
) -> dict:
    """Write the pass scope file the way a harden worker does; a None lead ref leaves --lead-ref out."""
    lead_arguments = [] if lead_ref is None else ["--lead-ref", lead_ref]
    result = _run_cli(
        [
            "pass-scope",
            *[argument for creation in creations for argument in ("--creation", creation)],
            "--repo-root",
            str(repo_root),
            "--diff-base",
            diff_base,
            *lead_arguments,
            "--out",
            str(scope_path),
        ]
    )
    assert result.exit_code == 0, result.output
    return json.loads(scope_path.read_text())


def _selected_commands(repo_root: Path, scope_path: Path) -> list[str]:
    result = _run_cli(["select-tests", "--repo-root", str(repo_root), "--scope-file", str(scope_path)])
    assert result.exit_code == 0, result.output
    return [line for line in result.output.splitlines() if not line.startswith("#")]


def test_a_pass_gate_runs_what_the_pass_changed_and_not_the_other_work_in_its_window(
    tmp_path: Path,
) -> None:
    """Another chat's commits that land between the diff base and the dispatch are in the pass's
    window, but none of their suites is the pass's to run: not the chat app's, and not the full
    root suite a flat script would bring in. The creation's change, the worker's edit outside its
    footprint, and what depends on that edit still run."""
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    diff_base = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    commit_changes(repo_root, {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n"}, "the chat's change")
    commit_changes(
        repo_root,
        {"system/apps/chat/imbue/chat/server.py": "PORT = 2\n", "system/scripts/forward_port.py": "PORT = 2\n"},
        "another chat's work",
    )
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(
        repo_root,
        {
            ".agents/skills/refresh/scripts/refresh_test.py": "def test_refresh_twice() -> None:\n    pass\n",
            "system/libs/midlib/src/midlib/core.py": "VALUE = 2\n",
        },
        "harden refresh",
    )
    # The lead's branch moves on while the worker works; that changes nothing about the pass.
    run_git(repo_root, ("checkout", "-q", "main"))
    commit_changes(repo_root, {"system/apps/chat/imbue/chat/server.py": "PORT = 3\n"}, "more of the other chat")
    run_git(repo_root, ("checkout", "-q", "worker"))
    scope_path = tmp_path / "scope.json"

    scope = _write_pass_scope(repo_root, scope_path, ["skill:refresh"], diff_base, "main")
    commands = _selected_commands(repo_root, scope_path)

    assert scope["diff"]["inside_footprint"] == [
        ".agents/skills/refresh/scripts/refresh.py",
        ".agents/skills/refresh/scripts/refresh_test.py",
    ]
    assert scope["diff"]["outside_footprint"] == ["system/libs/midlib/src/midlib/core.py"]
    assert scope["diff"]["others"] == ["system/apps/chat/imbue/chat/server.py", "system/scripts/forward_port.py"]
    assert commands[0].startswith("uv run pytest ")
    assert commands[1:] == [
        "uv run pytest .agents/skills/refresh",
        "uv run pytest system/apps/notes",
        "uv run pytest system/libs/midlib",
    ]


def test_a_pass_gate_runs_the_full_root_suite_for_an_unowned_path_the_worker_changed(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    diff_base = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    commit_changes(repo_root, {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n"}, "the chat's change")
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(repo_root, {"system/scripts/forward_port.py": "PORT = 2\n"}, "a fix the skill needed")
    scope_path = tmp_path / "scope.json"

    _write_pass_scope(repo_root, scope_path, ["skill:refresh"], diff_base, "main")
    commands = _selected_commands(repo_root, scope_path)

    assert "uv run pytest" in commands
    assert "uv run pytest .agents/skills/refresh" not in commands


def test_a_pass_gate_reads_its_lock_change_from_the_fork_point_not_the_diff_base(tmp_path: Path) -> None:
    """Another chat's upgrade in the window reaches corelib's dependents, but the pass's own lock
    change only adds a package, so the gate runs none of them."""
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    diff_base = commit_changes(repo_root, {"uv.lock": selection_lock("2.0")}, "lock")
    upgraded_lock = selection_lock("2.1")
    commit_changes(repo_root, {"uv.lock": upgraded_lock}, "another chat upgrades requests")
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    added_package = '\n[[package]]\nname = "newdep"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    commit_changes(repo_root, {"uv.lock": upgraded_lock + added_package}, "the pass adds a package")
    scope_path = tmp_path / "scope.json"

    scope = _write_pass_scope(repo_root, scope_path, ["skill:refresh"], diff_base, "main")
    commands = _selected_commands(repo_root, scope_path)

    assert scope["diff"]["outside_footprint"] == ["uv.lock"]
    assert "uv run pytest system/libs/corelib" not in commands
    assert "uv run pytest system/libs/midlib" not in commands


def test_a_pass_gate_runs_what_the_hardened_changes_own_lock_upgrade_reaches(tmp_path: Path) -> None:
    """The change being hardened upgraded a package before the worker forked, and the worker left
    the lock alone: that upgrade is still the pass's, so its dependents run."""
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    diff_base = commit_changes(repo_root, {"uv.lock": selection_lock("2.0")}, "lock")
    commit_changes(
        repo_root,
        {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n", "uv.lock": selection_lock("2.1")},
        "the chat's change, with the upgrade it needed",
    )
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(
        repo_root,
        {".agents/skills/refresh/scripts/refresh_test.py": "def test_refresh_twice() -> None:\n    pass\n"},
        "harden refresh",
    )
    scope_path = tmp_path / "scope.json"

    scope = _write_pass_scope(repo_root, scope_path, ["skill:refresh"], diff_base, "main")
    commands = _selected_commands(repo_root, scope_path)

    assert scope["diff"]["outside_footprint"] == ["uv.lock"]
    assert "uv run pytest system/libs/corelib" in commands
    assert "uv run pytest system/libs/midlib" in commands


@pytest.mark.parametrize(
    ("creation", "worker_change", "lead_change", "reached", "not_reached"),
    [
        (
            "app:notes",
            "system/apps/notes/src/notes/core.py",
            {"system/libs/midlib/src/midlib/core.py": "VALUE = 2\n"},
            ["uv run pytest system/apps/notes"],
            "uv run pytest system/libs/midlib",
        ),
        (
            "app:notes",
            "system/apps/notes/src/notes/core.py",
            {"uv.lock": selection_lock("2.1")},
            ["uv run pytest system/apps/notes"],
            "uv run pytest system/libs/corelib",
        ),
        (
            "app:notes",
            "system/apps/notes/src/notes/core.py",
            {"conftest.py": "import os\n"},
            ["uv run pytest system/apps/notes"],
            "uv run pytest system/libs",
        ),
        (
            "app:chat",
            "system/apps/chat/imbue/chat/server.py",
            {"system/libs/ui/src/index.ts": "export const x = 2;\n"},
            [
                "(cd system && npm test --workspace=apps/chat/frontend)",
                "(cd system/apps/chat && uv run pytest --no-cov -m 'browser or frontend')",
            ],
            "--workspace=libs/ui",
        ),
        (
            "skill:refresh",
            ".agents/skills/refresh/scripts/refresh.py",
            {"system/libs/midlib/src/midlib/core.py": "VALUE = 2\n"},
            ["uv run pytest .agents/skills/refresh"],
            "uv run pytest system/",
        ),
        (
            "skill:refresh",
            ".agents/skills/refresh/scripts/refresh.py",
            {"uv.lock": selection_lock("2.1")},
            ["uv run pytest .agents/skills/refresh"],
            "uv run pytest system/",
        ),
        (
            "skill:refresh",
            ".agents/skills/refresh/scripts/refresh.py",
            {"conftest.py": "import os\n"},
            ["uv run pytest .agents/skills/refresh"],
            "uv run pytest system/",
        ),
    ],
)
def test_a_catch_up_gate_runs_a_creations_own_suite_when_what_came_in_reaches_it(
    tmp_path: Path,
    creation: str,
    worker_change: str,
    lead_change: dict[str, str],
    reached: list[str],
    not_reached: str,
) -> None:
    """The lead's commits a worker merges in after its gate passed can change something its
    creation depends on without touching the creation: a package, a lock upgrade, a shared
    frontend, the root project's own configuration for a creation the root project collects, or
    for a skill, which runs on the root project, a package it depends on. The creation's own
    suite has never run against that change and the pass's together, so the catch-up gate runs
    it; the changed package's own suite, its other dependents, the full root suite, and the rest
    of what came in are not the pass's."""
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    declare_root_dependency(repo_root, "midlib")
    diff_base = commit_changes(repo_root, {"uv.lock": selection_lock("2.0")}, "lock")
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(repo_root, {worker_change: "VALUE = 2\n"}, "harden")
    run_git(repo_root, ("checkout", "-q", "main"))
    commit_changes(repo_root, {**lead_change, "system/scripts/forward_port.py": "PORT = 2\n"}, "other chats")
    run_git(repo_root, ("checkout", "-q", "worker"))
    pre = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    run_git(repo_root, ("merge", "-q", "--no-ff", "main", "-m", "Catch up"))
    scope_path = tmp_path / "catch-up-scope.json"

    scope = _write_pass_scope(repo_root, scope_path, [creation], pre, "main")
    commands = _selected_commands(repo_root, scope_path)
    whole_pass = _write_pass_scope(repo_root, tmp_path / "scope.json", [creation], diff_base, "main")

    assert sorted(scope["diff"]["incoming"]) == sorted([*lead_change, "system/scripts/forward_port.py"])
    assert set(reached) <= set(commands)
    assert not any(not_reached in command for command in commands)
    assert "uv run pytest" not in commands
    assert whole_pass["diff"]["incoming_base"] == diff_base


@pytest.mark.parametrize(
    ("lead_change", "runs_full_root"),
    [
        ({"conftest.py": "import os\n"}, True),
        ({"system/libs/midlib/src/midlib/core.py": "VALUE = 2\n"}, True),
        ({"uv.lock": selection_lock("2.1")}, True),
        ({"uv.lock": "not [ toml"}, True),
        ({"system/apps/notes/src/notes/core.py": "VALUE = 2\n"}, False),
    ],
)
def test_a_catch_up_gate_runs_the_full_root_suite_for_a_shared_script_what_came_in_reaches(
    tmp_path: Path, lead_change: dict[str, str], runs_full_root: bool
) -> None:
    """A shared script belongs to no package or skill: the scripts that import it are collected
    anywhere in the root project, so a change to it runs the full root suite. When the lead's
    work a catch-up brings in reaches it through the root project's configuration or something
    the root project depends on, the catch-up gate runs that suite for it too; work that reaches
    nothing it runs on adds nothing."""
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    declare_root_dependency(repo_root, "midlib")
    commit_changes(
        repo_root,
        {
            "uv.lock": selection_lock("2.0"),
            ".agents/shared/scripts/tool.py": "VALUE = 1\n",
            ".agents/shared/scripts/tool_test.py": "def test_tool() -> None:\n    pass\n",
        },
        "lock and shared script",
    )
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(repo_root, {".agents/shared/scripts/tool.py": "VALUE = 2\n"}, "harden")
    run_git(repo_root, ("checkout", "-q", "main"))
    commit_changes(repo_root, lead_change, "other chats")
    run_git(repo_root, ("checkout", "-q", "worker"))
    pre = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    run_git(repo_root, ("merge", "-q", "--no-ff", "main", "-m", "Catch up"))
    scope_path = tmp_path / "catch-up-scope.json"

    _write_pass_scope(repo_root, scope_path, ["shared:scripts/tool.py"], pre, "main")
    commands = _selected_commands(repo_root, scope_path)

    assert ("uv run pytest" in commands) == runs_full_root
    assert not any("system/apps/notes" in command for command in commands)


def test_a_catch_up_gate_runs_every_creations_own_suite_when_an_incoming_lock_cannot_be_read(
    tmp_path: Path,
) -> None:
    """With no way to tell what an incoming uv.lock upgraded, any creation may depend on it, so
    each creation's own suite runs, and the gate says why; nothing beyond the pass's runs."""
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    commit_changes(repo_root, {"uv.lock": selection_lock("2.0")}, "lock")
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(repo_root, {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n"}, "harden")
    run_git(repo_root, ("checkout", "-q", "main"))
    commit_changes(repo_root, {"uv.lock": "not [ toml", "system/scripts/forward_port.py": "PORT = 2\n"}, "other chats")
    run_git(repo_root, ("checkout", "-q", "worker"))
    pre = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    run_git(repo_root, ("merge", "-q", "--no-ff", "main", "-m", "Catch up"))
    scope_path = tmp_path / "catch-up-scope.json"
    _write_pass_scope(repo_root, scope_path, ["app:notes", "skill:refresh"], pre, "main")

    result = _run_cli(["select-tests", "--repo-root", str(repo_root), "--scope-file", str(scope_path)])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    commands = [line for line in lines if not line.startswith("#")]
    assert {"uv run pytest system/apps/notes", "uv run pytest .agents/skills/refresh"} <= set(commands)
    assert "uv run pytest" not in commands
    assert not any("system/libs" in command for command in commands)
    assert any(line.startswith("# note: ") and "every creation's own suite runs" in line for line in lines)


def test_a_pass_gate_runs_nothing_for_what_reached_its_creation_before_the_branch_departed(
    tmp_path: Path,
) -> None:
    """Another chat's change to a package the creation depends on, landed before the worker
    branched, is in the tree the worker's gate already ran on, so it adds nothing to that gate."""
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    diff_base = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    commit_changes(repo_root, {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n"}, "the chat's change")
    commit_changes(repo_root, {"system/libs/midlib/src/midlib/core.py": "VALUE = 2\n"}, "another chat")
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(
        repo_root,
        {".agents/skills/refresh/scripts/refresh_test.py": "def test_refresh_twice() -> None:\n    pass\n"},
        "harden refresh",
    )
    scope_path = tmp_path / "scope.json"

    scope = _write_pass_scope(repo_root, scope_path, ["skill:refresh", "app:notes"], diff_base, "main")
    commands = _selected_commands(repo_root, scope_path)

    assert scope["diff"]["incoming_base"] is None
    assert scope["diff"]["incoming"] == []
    assert "uv run pytest system/apps/notes" not in commands


def test_a_pass_gate_refuses_a_scope_file_that_no_longer_describes_the_tree(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    diff_base = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(repo_root, {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n"}, "harden refresh")
    scope_path = tmp_path / "scope.json"
    _write_pass_scope(repo_root, scope_path, ["skill:refresh"], diff_base, "main")
    write_repo_file(repo_root, "system/libs/midlib/src/midlib/core.py", "VALUE = 2\n")

    uncommitted = _run_cli(["select-tests", "--repo-root", str(repo_root), "--scope-file", str(scope_path)])
    head = commit_everything(repo_root, "a later fix")
    stale = _run_cli(["select-tests", "--repo-root", str(repo_root), "--scope-file", str(scope_path)])
    _write_pass_scope(repo_root, scope_path, ["skill:refresh"], diff_base, "main")
    regenerated = _selected_commands(repo_root, scope_path)

    assert uncommitted.exit_code != 0
    assert "system/libs/midlib/src/midlib/core.py" in uncommitted.output
    assert stale.exit_code != 0
    assert head in stale.output
    assert "uv run pytest system/libs/midlib" in regenerated


def test_a_pass_gate_refuses_a_scope_file_written_without_the_leads_branch(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    build_selection_workspace(repo_root)
    diff_base = run_git(repo_root, ("rev-parse", "HEAD")).strip()
    commit_changes(repo_root, {".agents/skills/refresh/scripts/refresh.py": "VALUE = 2\n"}, "the chat's change")
    scope_path = tmp_path / "scope.json"
    _write_pass_scope(repo_root, scope_path, ["skill:refresh"], diff_base, None)

    result = _run_cli(["select-tests", "--repo-root", str(repo_root), "--scope-file", str(scope_path)])

    assert result.exit_code != 0
    assert "--lead-ref" in result.output


def test_a_pass_gate_refuses_a_footprint_file_in_place_of_a_pass_scope_file(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    footprint_path = tmp_path / "footprint.json"
    written = _run_cli(
        ["footprint", "system/apps/news/app.toml", "--repo-root", str(tmp_path), "--out", str(footprint_path)]
    )
    assert written.exit_code == 0, written.output

    result = _run_cli(["select-tests", "--repo-root", str(tmp_path), "--scope-file", str(footprint_path)])

    assert result.exit_code != 0
    assert f"cannot read the pass scope file {footprint_path}" in result.output


def test_select_tests_needs_a_diff_or_paths(tmp_path: Path) -> None:
    build_selection_workspace(tmp_path)

    neither = _run_cli(["select-tests", "--repo-root", str(tmp_path)])
    ref_without_base = _run_cli(["select-tests", "--repo-root", str(tmp_path), "--diff-ref", "HEAD"])
    scope_file_and_base = _run_cli(
        [
            "select-tests",
            "--repo-root",
            str(tmp_path),
            "--scope-file",
            str(tmp_path / "scope.json"),
            "--diff-base",
            "HEAD",
        ]
    )

    assert neither.exit_code != 0
    assert "--diff-base" in neither.output
    assert ref_without_base.exit_code != 0
    assert scope_file_and_base.exit_code != 0
    assert "--scope-file names the change on its own" in scope_file_and_base.output


def test_pass_scope_needs_a_diff_base_for_a_lead_ref(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(
        ["pass-scope", "--creation", "app:news", "--repo-root", str(tmp_path), "--lead-ref", "HEAD"]
    )

    assert result.exit_code != 0
    assert "--lead-ref" in result.output


def test_pass_scope_judges_the_diff_against_every_creation_in_the_pass(tmp_path: Path) -> None:
    """A change spanning an app, an unrelated skill, a service, and a shared reference is all
    the pass's; only a file no creation accounts for is outside, and the service owns its
    programs by name."""
    build_news_workspace(tmp_path)
    write_repo_file(tmp_path, ".agents/skills/digest/SKILL.md", "# digest\n")
    write_repo_file(tmp_path, ".agents/shared/references/digest.md", "# digest\n")
    write_repo_file(tmp_path, "system/services/share_gateway/runner.py", "PORT = 1\n")
    write_supervisord_dropin(tmp_path, "share-gateway", ("program:share-gateway", "program:share-gateway-relay"))
    write_repo_file(tmp_path, "docs/system/unrelated.md", "# unrelated\n")
    init_git_repository(tmp_path)
    base_sha = commit_everything(tmp_path, "base")
    write_repo_file(tmp_path, "system/apps/news/runner.py", "ROUTES = ('/api/entries',)\n")
    write_repo_file(tmp_path, ".agents/skills/digest/SKILL.md", "# digest, edited\n")
    write_repo_file(tmp_path, ".agents/shared/references/digest.md", "# digest, edited\n")
    write_repo_file(tmp_path, "system/services/share_gateway/runner.py", "PORT = 2\n")
    write_supervisord_dropin(tmp_path, "share-gateway", ("program:share-gateway",))
    write_repo_file(tmp_path, "docs/system/unrelated.md", "# unrelated, edited\n")
    commit_everything(tmp_path, "the change under review")

    result = _run_cli(
        [
            "pass-scope",
            "--creation",
            "app:news",
            "--creation",
            "skill:digest",
            "--creation",
            "service:share_gateway",
            "--creation",
            "shared:references/digest.md",
            "--repo-root",
            str(tmp_path),
            "--diff-base",
            base_sha,
        ]
    )

    assert result.exit_code == 0, result.output
    scope = json.loads(result.output)
    assert [creation["creation"]["type"] for creation in scope["creations"]] == ["app", "skill", "service", "skill"]
    service = scope["creations"][2]
    assert service["primary"] == ["system/services/share_gateway/"]
    assert service["wiring"] == [
        {"path": "system/supervisord.conf.d/share-gateway.conf", "sections": ["program:share-gateway"]}
    ]
    assert sorted(scope["diff"]["inside_footprint"]) == [
        ".agents/shared/references/digest.md",
        ".agents/skills/digest/SKILL.md",
        "system/apps/news/runner.py",
        "system/services/share_gateway/runner.py",
        "system/supervisord.conf.d/share-gateway.conf",
    ]
    assert scope["diff"]["outside_footprint"] == ["docs/system/unrelated.md"]


def test_pass_scope_refuses_a_creation_that_is_not_type_and_name(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    result = _run_cli(["pass-scope", "--creation", "widget:news", "--repo-root", str(tmp_path)])

    assert result.exit_code != 0
    assert "widget:news" in result.output
