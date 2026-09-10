import json
from pathlib import Path

import pytest

from app_manifest.errors import ManifestLoadError
from app_manifest.errors import ScopeComputationError
from app_manifest.manifest import load_manifest
from app_manifest.primitives import ReferencePath
from app_manifest.primitives import RepoRelativePath
from app_manifest.scope import BUILT_IN_EXCLUDES
from app_manifest.scope import CreationScope
from app_manifest.scope import CreationType
from app_manifest.scope import ReferenceKind
from app_manifest.scope import compute_app_scope
from app_manifest.scope import compute_skill_scope
from app_manifest.scope import find_referencing_manifests
from app_manifest.scope import find_wiring_sections
from app_manifest.scope import is_path_covered_by
from app_manifest.scope import reference_kind_for_path
from app_manifest.scope import render_scope_file
from app_manifest.scope import with_diff_against_base
from app_manifest.testing import commit_everything
from app_manifest.testing import init_git_repository
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_repo_file
from app_manifest.testing import write_supervisord_conf

_NEWS_MANIFEST = """
name = "news"
display_name = "News"
icon = "icon.svg"

[[references]]
path = ".agents/skills/news-refresh"
note = "Fetches stories on a schedule; calls POST /api/ingest"

[[references]]
path = "system/scripts/run_news.sh"

[scope]
exclude = ["docs/generated/**", "data/**"]
"""


def _build_news_workspace(repo_root: Path) -> Path:
    """A repo-shaped tree holding the news app, everything it references, and a supervisord conf."""
    manifest_path = write_app_manifest(repo_root, "news", _NEWS_MANIFEST, is_icon_written=True)
    write_repo_file(repo_root, "system/apps/news/runner.py", "ROUTES = ('/api/ingest',)\n")
    write_repo_file(repo_root, ".agents/skills/news-refresh/SKILL.md", "# refresh\n")
    write_repo_file(repo_root, "system/scripts/run_news.sh", "#!/bin/sh\nexit 0\n")
    write_supervisord_conf(repo_root, ("program:news", "program:news-fetcher", "program:files"))
    return manifest_path


# --- reference kinds ------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference_path", "expected_kind"),
    [
        (".agents/skills/news-refresh", ReferenceKind.SKILL),
        (".agents/shared/references/spec-summary.md", ReferenceKind.SHARED),
        ("system/scripts/run_news.sh", ReferenceKind.SCRIPT),
        ("system/services/host_backup", ReferenceKind.SERVICE),
        ("docs/system/news.md", ReferenceKind.DOC),
        ("catalog/news.json", ReferenceKind.OTHER),
        (".agents/changelog/news.md", ReferenceKind.OTHER),
    ],
)
def test_reference_kind_comes_from_the_paths_prefix(
    reference_path: str, expected_kind: ReferenceKind
) -> None:
    assert reference_kind_for_path(ReferencePath(reference_path)) is expected_kind


# --- footprint coverage ---------------------------------------------------------


@pytest.mark.parametrize(
    ("footprint_entry", "candidate", "is_covered"),
    [
        ("system/apps/news/", "system/apps/news/runner.py", True),
        ("system/apps/news/", "system/apps/news_other/runner.py", False),
        (".agents/skills/news-refresh", ".agents/skills/news-refresh", True),
        (".agents/skills/news-refresh", ".agents/skills/news-refresh/SKILL.md", True),
        (".agents/skills/news-refresh", ".agents/skills/news-refresher/SKILL.md", False),
    ],
)
def test_a_footprint_entry_covers_itself_and_what_is_beneath_it(
    footprint_entry: str, candidate: str, is_covered: bool
) -> None:
    assert is_path_covered_by(footprint_entry, candidate) is is_covered


# --- wiring ---------------------------------------------------------------------


def test_wiring_finds_the_apps_program_and_its_sidecars_but_not_other_apps(
    tmp_path: Path,
) -> None:
    manifest_path = _build_news_workspace(tmp_path)
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    wiring = find_wiring_sections(tmp_path, manifest)

    assert len(wiring) == 1
    assert wiring[0].path == "system/supervisord.conf"
    assert list(wiring[0].sections) == ["program:news", "program:news-fetcher"]


def test_wiring_is_empty_for_an_app_the_supervisord_conf_does_not_run_yet(
    tmp_path: Path,
) -> None:
    manifest_path = _build_news_workspace(tmp_path)
    write_supervisord_conf(tmp_path, ("program:files", "program:chat"))
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    assert find_wiring_sections(tmp_path, manifest) == ()


def test_wiring_is_empty_when_there_is_no_supervisord_conf_at_all(tmp_path: Path) -> None:
    manifest_path = _build_news_workspace(tmp_path)
    (tmp_path / "system" / "supervisord.conf").unlink()
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    assert find_wiring_sections(tmp_path, manifest) == ()


def test_an_unparseable_supervisord_conf_raises_rather_than_yielding_no_wiring(
    tmp_path: Path,
) -> None:
    manifest_path = _build_news_workspace(tmp_path)
    (tmp_path / "system" / "supervisord.conf").write_text("[program:news\ncommand=x\n")
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    with pytest.raises(ScopeComputationError, match="supervisord.conf"):
        find_wiring_sections(tmp_path, manifest)


# --- the app scope --------------------------------------------------------------


def test_an_app_scope_carries_the_apps_own_paths_wiring_references_and_conventions(
    tmp_path: Path,
) -> None:
    manifest_path = _build_news_workspace(tmp_path)
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    scope = compute_app_scope(tmp_path, manifest_path, manifest)

    assert scope.creation.type is CreationType.APP
    assert scope.creation.name == "news"
    assert scope.creation.package == "news"
    assert scope.creation.manifest == "system/apps/news/app.toml"
    assert list(scope.primary) == ["system/apps/news/"]
    assert [reference.path for reference in scope.references] == [
        ".agents/skills/news-refresh",
        "system/scripts/run_news.sh",
    ]
    assert scope.references[0].kind is ReferenceKind.SKILL
    assert scope.references[0].note == "Fetches stories on a schedule; calls POST /api/ingest"
    assert scope.references[1].note is None
    assert scope.context == ()
    assert list(scope.conventions) == [
        "system/apps/README.md",
        ".agents/shared/worker/references/type-app.md",
        "docs/system/style_guide.md",
    ]
    assert scope.diff is None


def test_the_built_in_excludes_come_first_and_a_repeated_manifest_glob_is_dropped(
    tmp_path: Path,
) -> None:
    manifest_path = _build_news_workspace(tmp_path)
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    scope = compute_app_scope(tmp_path, manifest_path, manifest)

    # ``data/**`` is both a built-in and written in this manifest; it appears once, in built-in order.
    assert list(scope.exclude) == list(BUILT_IN_EXCLUDES) + ["docs/generated/**"]


def test_a_manifest_outside_the_repo_root_cannot_have_a_footprint(tmp_path: Path) -> None:
    manifest_path = _build_news_workspace(tmp_path / "workspace")
    manifest = load_manifest(manifest_path, repo_root=tmp_path / "workspace")

    with pytest.raises(ScopeComputationError, match="not inside the repo root"):
        compute_app_scope(tmp_path / "elsewhere", manifest_path, manifest)


def test_the_rendered_scope_file_is_indented_json_ending_in_a_newline(tmp_path: Path) -> None:
    manifest_path = _build_news_workspace(tmp_path)
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    rendered = render_scope_file(compute_app_scope(tmp_path, manifest_path, manifest))

    assert rendered.endswith("}\n")
    assert '\n  "primary": [' in rendered
    assert json.loads(rendered)["creation"] == {
        "type": "app",
        "name": "news",
        "package": "news",
        "manifest": "system/apps/news/app.toml",
    }


# --- the reverse lookup and the skill scope -------------------------------------


def test_the_reverse_lookup_finds_the_app_that_declares_a_referenced_directory(
    tmp_path: Path,
) -> None:
    _build_news_workspace(tmp_path)

    matches = find_referencing_manifests(tmp_path, RepoRelativePath(".agents/skills/news-refresh"))

    assert [match.manifest.name for match in matches] == ["news"]
    assert matches[0].reference.path == ".agents/skills/news-refresh"
    assert matches[0].manifest_path == tmp_path / "system" / "apps" / "news" / "app.toml"


def test_the_reverse_lookup_matches_a_file_beneath_a_referenced_directory(
    tmp_path: Path,
) -> None:
    _build_news_workspace(tmp_path)

    matches = find_referencing_manifests(
        tmp_path, RepoRelativePath(".agents/skills/news-refresh/SKILL.md")
    )

    assert [match.manifest.name for match in matches] == ["news"]


def test_the_reverse_lookup_returns_nothing_for_a_path_no_app_claims(tmp_path: Path) -> None:
    _build_news_workspace(tmp_path)

    assert find_referencing_manifests(tmp_path, RepoRelativePath(".agents/skills/unowned")) == ()


def test_the_reverse_lookup_skips_an_app_directory_with_no_manifest(tmp_path: Path) -> None:
    _build_news_workspace(tmp_path)
    write_repo_file(tmp_path, "system/apps/legacy/runner.py", "\n")

    matches = find_referencing_manifests(tmp_path, RepoRelativePath(".agents/skills/news-refresh"))

    assert [match.manifest.name for match in matches] == ["news"]


def test_the_reverse_lookup_raises_on_a_manifest_it_cannot_load(tmp_path: Path) -> None:
    _build_news_workspace(tmp_path)
    write_app_manifest(tmp_path, "broken", 'name = "broken"\n', is_icon_written=False)

    with pytest.raises(ManifestLoadError, match="broken"):
        find_referencing_manifests(tmp_path, RepoRelativePath(".agents/skills/news-refresh"))


def test_a_skill_scope_is_its_own_directory_plus_the_owning_apps_as_context(
    tmp_path: Path,
) -> None:
    _build_news_workspace(tmp_path)

    scope = compute_skill_scope(tmp_path, RepoRelativePath(".agents/skills/news-refresh"))

    assert scope.creation.type is CreationType.SKILL
    assert scope.creation.name == "news-refresh"
    assert scope.creation.package is None
    assert scope.creation.manifest is None
    assert list(scope.primary) == [".agents/skills/news-refresh/"]
    assert scope.wiring == ()
    assert scope.references == ()
    assert list(scope.context) == ["system/apps/news/"]
    assert list(scope.conventions) == [
        ".agents/shared/worker/references/type-skill.md",
        ".agents/shared/references/spec-summary.md",
        "docs/system/style_guide.md",
    ]
    assert scope.exclude == BUILT_IN_EXCLUDES


def test_a_skill_scope_for_a_single_file_keeps_the_file_as_its_primary_path(
    tmp_path: Path,
) -> None:
    _build_news_workspace(tmp_path)

    scope = compute_skill_scope(tmp_path, RepoRelativePath("system/scripts/run_news.sh"))

    assert list(scope.primary) == ["system/scripts/run_news.sh"]
    assert list(scope.context) == ["system/apps/news/"]


def test_a_skill_scope_for_a_path_no_app_claims_has_no_context(tmp_path: Path) -> None:
    _build_news_workspace(tmp_path)
    write_repo_file(tmp_path, ".agents/skills/unowned/SKILL.md", "# unowned\n")

    scope = compute_skill_scope(tmp_path, RepoRelativePath(".agents/skills/unowned"))

    assert scope.context == ()


# --- the diff -------------------------------------------------------------------


def _commit_news_workspace_base(repo_root: Path) -> str:
    _build_news_workspace(repo_root)
    write_repo_file(repo_root, "docs/system/style_guide.md", "# style\n")
    write_repo_file(repo_root, "docs/generated/api.md", "generated\n")
    write_repo_file(repo_root, "data/.apps/news/stories.json", "[]\n")
    write_repo_file(repo_root, "system/apps/files/runner.py", "\n")
    init_git_repository(repo_root)
    return commit_everything(repo_root, "base")


def _news_scope_with_diff(repo_root: Path, base_sha: str) -> CreationScope:
    manifest_path = repo_root / "system" / "apps" / "news" / "app.toml"
    manifest = load_manifest(manifest_path, repo_root=repo_root)
    scope = compute_app_scope(repo_root, manifest_path, manifest)
    return with_diff_against_base(scope, repo_root, base_sha)


def test_the_diff_lists_every_changed_file_and_only_the_unaccounted_ones_as_outside(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    base_sha = _commit_news_workspace_base(repo_root)
    write_repo_file(repo_root, "system/apps/news/runner.py", "ROUTES = ('/api/entries',)\n")
    write_repo_file(repo_root, ".agents/skills/news-refresh/SKILL.md", "# refresh, updated\n")
    write_repo_file(repo_root, "system/supervisord.conf", "[program:news]\ncommand=/bin/true\n")
    write_repo_file(repo_root, "system/apps/files/runner.py", "# unrelated app\n")
    commit_everything(repo_root, "the change under review")

    scope_with_diff = _news_scope_with_diff(repo_root, base_sha)

    assert scope_with_diff.diff is not None
    assert scope_with_diff.diff.base == base_sha
    assert sorted(scope_with_diff.diff.files) == [
        ".agents/skills/news-refresh/SKILL.md",
        "system/apps/files/runner.py",
        "system/apps/news/runner.py",
        "system/supervisord.conf",
    ]
    # The app directory, the referenced skill, and the owned supervisord block are all accounted
    # for; the other app's file is the one nothing in the footprint explains.
    assert list(scope_with_diff.diff.outside_footprint) == ["system/apps/files/runner.py"]


def test_an_excluded_file_is_never_reported_outside_the_footprint(tmp_path: Path) -> None:
    repo_root = tmp_path / "workspace"
    base_sha = _commit_news_workspace_base(repo_root)
    # One excluded file inside the app directory, and one excluded file outside every
    # footprint path -- the second is what only the exclude list can account for.
    write_repo_file(repo_root, "system/apps/news/static/bundle.js", "console.log(1)\n")
    write_repo_file(repo_root, "docs/generated/api.md", "regenerated\n")
    write_repo_file(repo_root, "data/.apps/news/stories.json", '[{"id": 1}]\n')
    commit_everything(repo_root, "regenerated output only")

    scope_with_diff = _news_scope_with_diff(repo_root, base_sha)

    assert scope_with_diff.diff is not None
    assert "docs/generated/api.md" in scope_with_diff.diff.files
    assert "system/apps/news/static/bundle.js" in scope_with_diff.diff.files
    assert scope_with_diff.diff.outside_footprint == ()


def test_a_diff_base_that_does_not_resolve_raises_rather_than_reporting_no_changes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    _commit_news_workspace_base(repo_root)
    manifest_path = repo_root / "system" / "apps" / "news" / "app.toml"
    manifest = load_manifest(manifest_path, repo_root=repo_root)
    scope = compute_app_scope(repo_root, manifest_path, manifest)

    with pytest.raises(ScopeComputationError, match="rev-parse"):
        with_diff_against_base(scope, repo_root, "no-such-ref-9f13c2")
