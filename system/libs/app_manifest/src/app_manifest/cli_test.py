import json
from pathlib import Path

from click.testing import CliRunner, Result

from app_manifest.cli import app_manifest_cli
from app_manifest.testing import (
    commit_everything,
    init_git_repository,
    write_app_manifest,
    write_repo_file,
    write_supervisord_conf,
)

_ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M2 2h20v20H2z"/></svg>'


def test_validate_manifest_accepts_a_valid_manifest(tmp_path: Path) -> None:
    (tmp_path / "icon.svg").write_text(_ICON)
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n')

    result = CliRunner().invoke(app_manifest_cli, ["validate-manifest", str(manifest_path)])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "ok: news (News)"


def test_validate_manifest_reports_the_failing_field_and_exits_non_zero(tmp_path: Path) -> None:
    manifest_path = tmp_path / "app.toml"
    manifest_path.write_text('name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\nbogus = 1\n')

    result = CliRunner().invoke(app_manifest_cli, ["validate-manifest", str(manifest_path)])

    assert result.exit_code != 0
    assert "bogus" in result.output


# --- footprint and references ---------------------------------------------------

_NEWS_MANIFEST = """
name = "news"
display_name = "News"
icon = "icon.svg"

[[references]]
path = ".agents/skills/news-refresh"
note = "Fetches stories on a schedule; calls POST /api/ingest"

[scope]
exclude = ["docs/generated/**"]
"""


def _build_news_workspace(repo_root: Path) -> Path:
    manifest_path = write_app_manifest(repo_root, "news", _NEWS_MANIFEST, is_icon_written=True)
    write_repo_file(repo_root, "system/apps/news/runner.py", "ROUTES = ('/api/ingest',)\n")
    write_repo_file(repo_root, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")
    write_supervisord_conf(repo_root, ("program:news", "program:news-fetcher"))
    return manifest_path


def _run_cli(arguments: list[str]) -> Result:
    return CliRunner().invoke(app_manifest_cli, arguments)


def test_footprint_writes_the_scope_file_for_an_app_to_stdout(tmp_path: Path) -> None:
    _build_news_workspace(tmp_path)

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
        }
    ]
    assert scope["context"] == []
    assert scope["exclude"][-1] == "docs/generated/**"
    assert scope["diff"] is None


def test_footprint_writes_the_scope_file_to_out_making_the_directories_above_it(
    tmp_path: Path,
) -> None:
    _build_news_workspace(tmp_path)
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
    _build_news_workspace(tmp_path)

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
    _build_news_workspace(tmp_path)

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
    _build_news_workspace(tmp_path)
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
    _build_news_workspace(tmp_path)
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
    assert diff["outside_footprint"] == ["docs/system/unrelated.md"]


def test_references_prints_one_json_object_per_app_that_owns_the_path(tmp_path: Path) -> None:
    _build_news_workspace(tmp_path)

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
    _build_news_workspace(tmp_path)

    result = _run_cli(
        ["references", "--for-path", ".agents/skills/unowned", "--repo-root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_references_reports_a_manifest_it_cannot_load(tmp_path: Path) -> None:
    _build_news_workspace(tmp_path)
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

    assert result.exit_code != 0
    assert "broken" in result.output
