import json
from pathlib import Path

from click.testing import CliRunner
from click.testing import Result

from app_manifest.cli import app_manifest_cli
from app_manifest.testing import APP_ICON_MARKUP
from app_manifest.testing import build_news_workspace
from app_manifest.testing import build_selection_workspace
from app_manifest.testing import commit_everything
from app_manifest.testing import init_git_repository
from app_manifest.testing import run_git
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_repo_file


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
    for relative_path, content in changes.items():
        write_repo_file(repo_root, relative_path, content)
    commit_everything(repo_root, "work")


def test_select_tests_over_explicit_paths_matches_the_same_set_read_from_a_diff(
    tmp_path: Path,
) -> None:
    build_selection_workspace(tmp_path)
    base_lock = _selection_lock("2.0")
    write_repo_file(tmp_path, "uv.lock", base_lock)
    commit_everything(tmp_path, "lock")
    changes = {
        "system/libs/midlib/src/midlib/core.py": "VALUE = 2\n",
        "system/scripts/forward_port.py": "PORT = 2\n",
        "uv.lock": _selection_lock("2.1"),
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
    assert selection["unclassified"] == ["unknown.bin"]
    assert selection["is_full_root"] is True
    assert [command["argv"] for command in selection["commands"]] == [["uv", "run", "pytest"]]
    assert {entry["path"]: entry["classes"] for entry in selection["paths"]} == {
        "system/scripts/forward_port.py": ["paired_script"],
        "unknown.bin": ["unclassified"],
    }


def test_select_tests_needs_a_diff_or_paths(tmp_path: Path) -> None:
    build_selection_workspace(tmp_path)

    neither = _run_cli(["select-tests", "--repo-root", str(tmp_path)])
    ref_without_base = _run_cli(["select-tests", "--repo-root", str(tmp_path), "--diff-ref", "HEAD"])

    assert neither.exit_code != 0
    assert "--diff-base" in neither.output
    assert ref_without_base.exit_code != 0


def _selection_lock(requests_version: str) -> str:
    return f"""
version = 1

[[package]]
name = "corelib"
version = "0.1.0"
source = {{ editable = "system/libs/corelib" }}
dependencies = [{{ name = "requests" }}]

[[package]]
name = "requests"
version = "{requests_version}"
source = {{ registry = "https://pypi.org/simple" }}
"""
