import json
from pathlib import Path

import pytest
from loguru import logger

from app_manifest.errors import ScopeComputationError
from app_manifest.manifest import AppManifest
from app_manifest.manifest import load_manifest
from app_manifest.primitives import ReferencePath
from app_manifest.primitives import RepoRelativePath
from app_manifest.primitives import is_path_covered_by
from app_manifest.scope import BUILT_IN_EXCLUDES
from app_manifest.scope import CreationScope
from app_manifest.scope import CreationType
from app_manifest.scope import ReferenceKind
from app_manifest.scope import compute_app_scope
from app_manifest.scope import compute_skill_scope
from app_manifest.scope import find_referencing_manifests
from app_manifest.scope import find_wiring_sections
from app_manifest.scope import reference_kind_for_path
from app_manifest.scope import render_scope_file
from app_manifest.scope import with_diff_against_base
from app_manifest.testing import NEWS_MANIFEST
from app_manifest.testing import build_news_workspace
from app_manifest.testing import commit_everything
from app_manifest.testing import init_git_repository
from app_manifest.testing import run_git
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_repo_file
from app_manifest.testing import write_supervisord_conf


# --- the news workspace every case below is built on ----------------------------


def _news_manifest_path(repo_root: Path) -> Path:
    return repo_root / "system" / "apps" / "news" / "app.toml"


def _news_manifest(repo_root: Path) -> AppManifest:
    return load_manifest(_news_manifest_path(repo_root), repo_root=repo_root)


def _news_app_scope(repo_root: Path) -> CreationScope:
    return compute_app_scope(repo_root, _news_manifest_path(repo_root), _news_manifest(repo_root))


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
    build_news_workspace(tmp_path)

    wiring = find_wiring_sections(tmp_path, _news_manifest(tmp_path))

    assert len(wiring) == 1
    assert wiring[0].path == "system/supervisord.conf"
    assert list(wiring[0].sections) == ["program:news", "program:news-fetcher"]


def test_wiring_is_empty_for_an_app_the_supervisord_conf_does_not_run_yet(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)
    write_supervisord_conf(tmp_path, ("program:files", "program:chat"))

    assert find_wiring_sections(tmp_path, _news_manifest(tmp_path)) == ()


def test_wiring_is_empty_when_there_is_no_supervisord_conf_at_all(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    (tmp_path / "system" / "supervisord.conf").unlink()

    assert find_wiring_sections(tmp_path, _news_manifest(tmp_path)) == ()


def test_an_unparseable_supervisord_conf_raises_rather_than_yielding_no_wiring(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)
    (tmp_path / "system" / "supervisord.conf").write_text("[program:news\ncommand=x\n")

    with pytest.raises(ScopeComputationError, match="supervisord.conf"):
        find_wiring_sections(tmp_path, _news_manifest(tmp_path))


# --- the app scope --------------------------------------------------------------


def test_an_app_scope_carries_the_apps_own_paths_wiring_references_and_conventions(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)

    scope = _news_app_scope(tmp_path)

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
    build_news_workspace(tmp_path)

    scope = _news_app_scope(tmp_path)

    # ``data/**`` is both a built-in and written in this manifest; it appears once, in built-in order.
    assert list(scope.exclude) == list(BUILT_IN_EXCLUDES) + ["docs/generated/**"]


@pytest.mark.parametrize("exclude_glob", ["system/apps/news/**", ".agents/skills/news-refresh/**"])
def test_an_exclude_that_swallows_a_footprint_entry_is_refused(tmp_path: Path, exclude_glob: str) -> None:
    # Excluding the app's own directory or a referenced skill whole would leave
    # outside_footprint empty no matter what changed; carving out a subdirectory is fine.
    build_news_workspace(tmp_path)
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[[references]]\npath = ".agents/skills/news-refresh"\n'
        f'[scope]\nexclude = ["{exclude_glob}"]\n',
        is_icon_written=True,
    )
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    with pytest.raises(ScopeComputationError, match="part of the footprint"):
        compute_app_scope(tmp_path, manifest_path, manifest)


def test_an_exclude_that_carves_out_a_subdirectory_is_accepted(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    write_repo_file(tmp_path, "system/apps/news/frontend/dist/bundle.js", "x\n")
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n'
        '[scope]\nexclude = ["system/apps/news/frontend/dist/**"]\n',
        is_icon_written=True,
    )
    manifest = load_manifest(manifest_path, repo_root=tmp_path)

    scope = compute_app_scope(tmp_path, manifest_path, manifest)

    assert "system/apps/news/frontend/dist/**" in scope.exclude


def test_an_unresolved_repo_root_is_resolved_before_use(tmp_path: Path) -> None:
    # macOS hands out /tmp for /private/tmp; a caller passing the unresolved form must
    # get the same footprint as one passing the resolved form.
    build_news_workspace(tmp_path.resolve())
    unresolved_root = Path(str(tmp_path).replace(str(tmp_path.resolve()), str(tmp_path), 1))

    scope = compute_app_scope(
        unresolved_root, _news_manifest_path(unresolved_root), _news_manifest(unresolved_root)
    )

    assert list(scope.primary) == ["system/apps/news/"]


def test_a_manifest_outside_the_repo_root_cannot_have_a_footprint(tmp_path: Path) -> None:
    manifest_path = build_news_workspace(tmp_path / "workspace")
    manifest = load_manifest(manifest_path, repo_root=tmp_path / "workspace")

    with pytest.raises(ScopeComputationError, match="not inside the repo root"):
        compute_app_scope(tmp_path / "elsewhere", manifest_path, manifest)


def test_the_rendered_scope_file_is_indented_json_ending_in_a_newline(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    rendered = render_scope_file(_news_app_scope(tmp_path))

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
    build_news_workspace(tmp_path)

    matches = find_referencing_manifests(tmp_path, RepoRelativePath(".agents/skills/news-refresh"))

    assert [match.manifest.name for match in matches] == ["news"]
    assert matches[0].reference.path == ".agents/skills/news-refresh"
    assert matches[0].manifest_path == tmp_path / "system" / "apps" / "news" / "app.toml"


def test_the_reverse_lookup_matches_a_file_beneath_a_referenced_directory(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)

    matches = find_referencing_manifests(
        tmp_path, RepoRelativePath(".agents/skills/news-refresh/SKILL.md")
    )

    assert [match.manifest.name for match in matches] == ["news"]


def test_the_reverse_lookup_returns_nothing_for_a_path_no_app_claims(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    assert find_referencing_manifests(tmp_path, RepoRelativePath(".agents/skills/unowned")) == ()


def test_the_reverse_lookup_skips_an_app_directory_with_no_manifest(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    write_repo_file(tmp_path, "system/apps/legacy/runner.py", "\n")

    matches = find_referencing_manifests(tmp_path, RepoRelativePath(".agents/skills/news-refresh"))

    assert [match.manifest.name for match in matches] == ["news"]


def test_the_reverse_lookup_skips_a_manifest_it_cannot_load_and_warns(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    write_app_manifest(tmp_path, "broken", 'name = "broken"\n', is_icon_written=False)
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="WARNING")
    try:
        matches = find_referencing_manifests(
            tmp_path, RepoRelativePath(".agents/skills/news-refresh")
        )
    finally:
        logger.remove(sink_id)

    # One app's stale manifest must not hide every other app's claim on the skill.
    assert [match.manifest.name for match in matches] == ["news"]
    assert len(captured) == 1
    assert "broken" in captured[0]
    assert "display_name" in captured[0]


def test_a_skill_scope_is_its_own_directory_plus_the_owning_apps_as_context(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)

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
    build_news_workspace(tmp_path)

    scope = compute_skill_scope(tmp_path, RepoRelativePath("system/scripts/run_news.sh"))

    assert list(scope.primary) == ["system/scripts/run_news.sh"]
    assert list(scope.context) == ["system/apps/news/"]


def test_a_skill_scope_refuses_a_path_that_does_not_exist(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)

    # git ignores a pathspec that matches nothing, so a mistyped path would otherwise read
    # as a creation whose every file is unchanged.
    with pytest.raises(ScopeComputationError, match="does not exist"):
        compute_skill_scope(tmp_path, RepoRelativePath(".agents/skills/news-refersh"))


def test_a_skill_scope_for_a_path_no_app_claims_has_no_context(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    write_repo_file(tmp_path, ".agents/skills/unowned/SKILL.md", "# unowned\n")

    scope = compute_skill_scope(tmp_path, RepoRelativePath(".agents/skills/unowned"))

    assert scope.context == ()


def test_a_change_to_the_owning_apps_manifest_is_inside_a_skill_scopes_footprint(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    build_news_workspace(repo_root)
    init_git_repository(repo_root)
    base_sha = commit_everything(repo_root, "base")
    write_repo_file(repo_root, ".agents/skills/news-refresh/SKILL.md", "# refresh, updated\n")
    write_app_manifest(
        repo_root,
        "news",
        NEWS_MANIFEST + '\n[[references]]\npath = "docs/system/news.md"\n',
        is_icon_written=True,
    )
    write_repo_file(repo_root, "docs/system/news.md", "# news\n")
    # A change to the owning app's code is not the skill's to make, so it stays outside.
    write_repo_file(repo_root, "system/apps/news/runner.py", "ROUTES = ('/api/ingest', '/api/x')\n")
    commit_everything(repo_root, "the skill claims a doc through its owning app")

    scope = compute_skill_scope(repo_root, RepoRelativePath(".agents/skills/news-refresh"))
    scope_with_diff = with_diff_against_base(scope, repo_root, base_sha)

    assert scope_with_diff.diff is not None
    assert "system/apps/news/app.toml" in scope_with_diff.diff.files
    # Only the owning app's manifest counts as inside the skill's footprint.
    assert scope_with_diff.diff.outside_footprint == (
        "docs/system/news.md",
        "system/apps/news/runner.py",
    )


# --- the diff -------------------------------------------------------------------


def _commit_news_workspace_base(repo_root: Path) -> str:
    build_news_workspace(repo_root)
    write_repo_file(repo_root, "docs/system/style_guide.md", "# style\n")
    write_repo_file(repo_root, "docs/generated/api.md", "generated\n")
    write_repo_file(repo_root, "data/.apps/news/stories.json", "[]\n")
    write_repo_file(repo_root, "system/apps/files/runner.py", "\n")
    init_git_repository(repo_root)
    return commit_everything(repo_root, "base")


def _news_scope_with_diff(repo_root: Path, base_sha: str) -> CreationScope:
    return with_diff_against_base(_news_app_scope(repo_root), repo_root, base_sha)


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
    write_repo_file(repo_root, "system/apps/news/frontend/dist/bundle.js", "console.log(1)\n")
    write_repo_file(repo_root, "docs/generated/api.md", "regenerated\n")
    write_repo_file(repo_root, "data/.apps/news/stories.json", '[{"id": 1}]\n')
    commit_everything(repo_root, "regenerated output only")

    scope_with_diff = _news_scope_with_diff(repo_root, base_sha)

    assert scope_with_diff.diff is not None
    assert "docs/generated/api.md" in scope_with_diff.diff.files
    assert "system/apps/news/frontend/dist/bundle.js" in scope_with_diff.diff.files
    assert scope_with_diff.diff.outside_footprint == ()


def test_a_diff_base_that_does_not_resolve_raises_rather_than_reporting_no_changes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    _commit_news_workspace_base(repo_root)

    with pytest.raises(ScopeComputationError, match="rev-parse"):
        with_diff_against_base(_news_app_scope(repo_root), repo_root, "no-such-ref-9f13c2")


def test_the_diff_reports_a_non_ascii_name_and_a_name_with_a_space_as_the_paths_they_are(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    base_sha = _commit_news_workspace_base(repo_root)
    write_repo_file(repo_root, "system/apps/news/caf\u00e9.md", "# accented\n")
    write_repo_file(repo_root, "system/apps/news/two words.md", "# spaced\n")
    commit_everything(repo_root, "files whose names git would otherwise quote")

    scope_with_diff = _news_scope_with_diff(repo_root, base_sha)

    # Without core.quotePath=false and -z, git renders the first as "caf\303\251.md", which is
    # not a path at all, and RepoRelativePath refuses the backslashes outright.
    assert scope_with_diff.diff is not None
    assert sorted(scope_with_diff.diff.files) == [
        "system/apps/news/caf\u00e9.md",
        "system/apps/news/two words.md",
    ]
    assert scope_with_diff.diff.outside_footprint == ()


def test_the_diff_against_a_diverged_base_reports_only_the_branchs_own_changes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    _commit_news_workspace_base(repo_root)
    run_git(repo_root, ("checkout", "-q", "-b", "feature"))
    write_repo_file(repo_root, "system/apps/news/runner.py", "ROUTES = ('/api/entries',)\n")
    commit_everything(repo_root, "the change under review")
    run_git(repo_root, ("checkout", "-q", "main"))
    write_repo_file(repo_root, "docs/system/style_guide.md", "# style, advanced after the fork\n")
    commit_everything(repo_root, "the base branch moves on")
    run_git(repo_root, ("checkout", "-q", "feature"))

    scope_with_diff = _news_scope_with_diff(repo_root, "main")

    # The three-dot form diffs from the merge base, so what the base branch did after the
    # fork is not this creation's change.
    assert scope_with_diff.diff is not None
    assert list(scope_with_diff.diff.files) == ["system/apps/news/runner.py"]
    assert scope_with_diff.diff.outside_footprint == ()
