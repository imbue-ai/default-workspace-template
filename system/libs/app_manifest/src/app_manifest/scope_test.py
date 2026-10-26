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
from app_manifest.scope import compute_creation_scope
from app_manifest.scope import compute_pass_scope
from app_manifest.scope import compute_skill_scope
from app_manifest.scope import find_referencing_manifests
from app_manifest.scope import find_wiring_sections
from app_manifest.scope import parse_creation_spec
from app_manifest.scope import reference_kind_for_path
from app_manifest.scope import render_scope_file
from app_manifest.scope import split_against_creations
from app_manifest.scope import with_diff_against_base
from app_manifest.testing import NEWS_MANIFEST
from app_manifest.testing import build_news_workspace
from app_manifest.testing import commit_changes
from app_manifest.testing import commit_everything
from app_manifest.testing import init_git_repository
from app_manifest.testing import run_git
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_repo_file
from app_manifest.testing import write_supervisord_conf
from app_manifest.testing import write_supervisord_dropin


def _news_manifest_path(repo_root: Path) -> Path:
    return repo_root / "system" / "apps" / "news" / "app.toml"


def _news_manifest(repo_root: Path) -> AppManifest:
    return load_manifest(_news_manifest_path(repo_root), repo_root=repo_root)


def _news_app_scope(repo_root: Path) -> CreationScope:
    return compute_app_scope(repo_root, _news_manifest_path(repo_root), _news_manifest(repo_root))


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


def test_wiring_finds_the_apps_program_and_its_sidecars_but_not_other_apps(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)

    wiring = find_wiring_sections(tmp_path, _news_manifest(tmp_path))

    assert len(wiring) == 1
    assert wiring[0].path == "system/supervisord.conf"
    assert list(wiring[0].sections) == ["program:news", "program:news-fetcher"]


def test_wiring_includes_the_programs_the_manifest_declares(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    write_supervisord_conf(tmp_path, ("program:news", "program:xvfb", "program:files"))
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n[wiring]\nprograms = ["xvfb"]\n',
        is_icon_written=True,
    )

    wiring = find_wiring_sections(tmp_path, load_manifest(manifest_path, repo_root=tmp_path))

    assert list(wiring[0].sections) == ["program:news", "program:xvfb"]


def test_wiring_finds_blocks_in_the_drop_ins_the_include_glob_names(tmp_path: Path) -> None:
    # The template declares every program in its own drop-in file, so a reader of the main
    # config alone attributes no wiring at all to any app.
    build_news_workspace(tmp_path)
    write_supervisord_conf(tmp_path, ())
    write_supervisord_dropin(tmp_path, "news", ("program:news", "program:news-fetcher"))

    wiring = find_wiring_sections(tmp_path, _news_manifest(tmp_path))

    assert len(wiring) == 1
    assert wiring[0].path == "system/supervisord.conf.d/news.conf"
    assert list(wiring[0].sections) == ["program:news", "program:news-fetcher"]


def test_wiring_attributes_each_block_to_the_file_it_is_written_in(tmp_path: Path) -> None:
    # A footprint names the file a change would have to edit, so a sidecar living in its
    # own drop-in cannot be reported against the file that merely includes it.
    build_news_workspace(tmp_path)
    write_supervisord_conf(tmp_path, ())
    write_supervisord_dropin(tmp_path, "news", ("program:news",))
    write_supervisord_dropin(tmp_path, "xvfb", ("program:xvfb",))
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n[wiring]\nprograms = ["xvfb"]\n',
        is_icon_written=True,
    )

    wiring = find_wiring_sections(tmp_path, load_manifest(manifest_path, repo_root=tmp_path))

    assert [(section.path, list(section.sections)) for section in wiring] == [
        ("system/supervisord.conf.d/news.conf", ["program:news"]),
        ("system/supervisord.conf.d/xvfb.conf", ["program:xvfb"]),
    ]


def test_a_declared_wiring_program_with_no_block_is_an_error(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    manifest_path = write_app_manifest(
        tmp_path,
        "news",
        'name = "news"\ndisplay_name = "News"\nicon = "icon.svg"\n[wiring]\nprograms = ["xvfb"]\n',
        is_icon_written=True,
    )

    with pytest.raises(ScopeComputationError, match="program:xvfb"):
        find_wiring_sections(tmp_path, load_manifest(manifest_path, repo_root=tmp_path))


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
    scope_with_diff = with_diff_against_base(scope, repo_root, base_sha, "HEAD")

    assert scope_with_diff.diff is not None
    assert "system/apps/news/app.toml" in scope_with_diff.diff.files
    # Only the owning app's manifest counts as inside the skill's footprint.
    assert scope_with_diff.diff.inside_footprint == (
        ".agents/skills/news-refresh/SKILL.md",
        "system/apps/news/app.toml",
    )
    assert scope_with_diff.diff.outside_footprint == (
        "docs/system/news.md",
        "system/apps/news/runner.py",
    )


def _commit_news_workspace_base(repo_root: Path) -> str:
    build_news_workspace(repo_root)
    write_repo_file(repo_root, "docs/system/style_guide.md", "# style\n")
    write_repo_file(repo_root, "docs/generated/api.md", "generated\n")
    write_repo_file(repo_root, "data/.apps/news/stories.json", "[]\n")
    write_repo_file(repo_root, "system/apps/files/runner.py", "\n")
    init_git_repository(repo_root)
    return commit_everything(repo_root, "base")


def _news_scope_with_diff(repo_root: Path, base_sha: str) -> CreationScope:
    return with_diff_against_base(_news_app_scope(repo_root), repo_root, base_sha, "HEAD")


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
    assert list(scope_with_diff.diff.inside_footprint) == [
        ".agents/skills/news-refresh/SKILL.md",
        "system/apps/news/runner.py",
        "system/supervisord.conf",
    ]
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
    # Excluded on both sides: the bundle sits under the app directory but is not the
    # creation's content either.
    assert scope_with_diff.diff.inside_footprint == ()
    assert scope_with_diff.diff.outside_footprint == ()


def test_a_diff_base_that_does_not_resolve_raises_rather_than_reporting_no_changes(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "workspace"
    _commit_news_workspace_base(repo_root)

    with pytest.raises(ScopeComputationError, match="rev-parse"):
        with_diff_against_base(_news_app_scope(repo_root), repo_root, "no-such-ref-9f13c2", "HEAD")


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


def test_the_diff_can_run_to_a_ref_other_than_head_so_a_merge_answers_for_each_side(
    tmp_path: Path,
) -> None:
    # The update-self worker stands on a merge commit and asks two questions of one tree:
    # what the workspace itself changed since the fork (base to the merge's first parent),
    # and what the update brought in (first parent to the merge). Both are the same
    # footprint read over a different range.
    repo_root = tmp_path / "workspace"
    base_sha = _commit_news_workspace_base(repo_root)
    run_git(repo_root, ("checkout", "-q", "-b", "upstream"))
    write_repo_file(repo_root, "system/scripts/run_news.sh", "#!/bin/sh\nexit 1\n")
    write_repo_file(repo_root, "docs/system/style_guide.md", "# style, revised upstream\n")
    commit_everything(repo_root, "the release")
    run_git(repo_root, ("checkout", "-q", "main"))
    write_repo_file(repo_root, "system/apps/news/extra.py", "MINE = True\n")
    local_sha = commit_everything(repo_root, "the workspace's own work")
    run_git(repo_root, ("merge", "-q", "--no-ff", "-m", "update-self: merge upstream", "upstream"))

    local_side = with_diff_against_base(_news_app_scope(repo_root), repo_root, base_sha, "HEAD^1")
    update_side = with_diff_against_base(_news_app_scope(repo_root), repo_root, "HEAD^1", "HEAD")

    assert local_side.diff is not None
    assert local_side.diff.ref == local_sha
    assert local_side.diff.files == ("system/apps/news/extra.py",)
    assert local_side.diff.inside_footprint == ("system/apps/news/extra.py",)
    assert update_side.diff is not None
    assert update_side.diff.base == local_sha
    assert sorted(update_side.diff.files) == [
        "docs/system/style_guide.md",
        "system/scripts/run_news.sh",
    ]
    # The referenced script the release changed is inside; the doc the app never claimed is not.
    assert update_side.diff.inside_footprint == ("system/scripts/run_news.sh",)
    assert update_side.diff.outside_footprint == ("docs/system/style_guide.md",)


@pytest.mark.parametrize(
    "spec",
    [
        "news",
        "skill:",
        "widget:news",
        "skill:digest/scripts",
        "shared:../skills/digest",
        "app:.",
        "shared:references/",
    ],
)
def test_a_creation_spec_is_refused_unless_it_is_type_and_name_inside_its_directory(spec: str) -> None:
    with pytest.raises(ScopeComputationError, match="is not <type>:<name>"):
        parse_creation_spec(spec)


def test_an_app_creation_is_found_by_its_registered_name_or_scoped_by_its_directory(
    tmp_path: Path,
) -> None:
    """A lead names an app by its registered name, which need not be its package's; an app
    directory with no manifest yet is scoped by its directory and the programs named after it."""
    build_news_workspace(tmp_path)
    write_app_manifest(
        tmp_path,
        "daily_digest",
        'name = "daily-digest"\ndisplay_name = "Daily digest"\nicon = "icon.svg"\n',
        is_icon_written=True,
    )
    write_repo_file(tmp_path, "system/apps/road_map/runner.py", "ROUTES = ()\n")
    write_supervisord_dropin(
        tmp_path, "road-map", ("program:road-map", "program:road-map-sync", "program:road-mapper")
    )

    by_name = compute_creation_scope(tmp_path, parse_creation_spec("app:daily-digest"))
    unmanifested = compute_creation_scope(tmp_path, parse_creation_spec("app:road_map"))

    assert by_name.creation.type is CreationType.APP
    assert by_name.creation.package == "daily_digest"
    assert by_name.creation.manifest == "system/apps/daily_digest/app.toml"
    assert list(by_name.primary) == ["system/apps/daily_digest/"]
    assert unmanifested.creation.type is CreationType.APP
    assert unmanifested.creation.manifest is None
    assert list(unmanifested.primary) == ["system/apps/road_map/"]
    assert [(wiring.path, list(wiring.sections)) for wiring in unmanifested.wiring] == [
        ("system/supervisord.conf.d/road-map.conf", ["program:road-map", "program:road-map-sync"])
    ]


def test_a_pass_diff_counts_a_file_one_creation_excludes_when_another_owns_it(tmp_path: Path) -> None:
    """A file is the pass's when any creation's footprint covers it and that creation does not
    exclude it; a file outside every footprint that some creation excludes is in neither list."""
    build_news_workspace(tmp_path)
    digest_manifest_path = write_app_manifest(
        tmp_path,
        "daily_digest",
        'name = "daily-digest"\ndisplay_name = "Daily digest"\nicon = "icon.svg"\n\n'
        '[scope]\nexclude = ["system/apps/news/runner.py"]\n',
        is_icon_written=True,
    )
    digest_scope = compute_app_scope(
        tmp_path, digest_manifest_path, load_manifest(digest_manifest_path, repo_root=tmp_path)
    )

    inside, outside = split_against_creations(
        [
            RepoRelativePath("system/apps/news/runner.py"),
            RepoRelativePath("system/apps/daily_digest/runner.py"),
            RepoRelativePath("docs/generated/api.md"),
            RepoRelativePath("docs/system/unrelated.md"),
        ],
        (_news_app_scope(tmp_path), digest_scope),
    )

    assert inside == ("system/apps/news/runner.py", "system/apps/daily_digest/runner.py")
    assert outside == ("docs/system/unrelated.md",)


def test_a_pass_that_names_a_creation_twice_is_refused(tmp_path: Path) -> None:
    build_news_workspace(tmp_path)
    news = parse_creation_spec("app:news")

    with pytest.raises(ScopeComputationError, match="names app:news more than once"):
        compute_pass_scope(tmp_path, [news, parse_creation_spec("skill:news-refresh"), news], None, "HEAD", None)


def test_a_pass_that_names_one_app_by_its_registered_name_and_by_its_package_is_refused(
    tmp_path: Path,
) -> None:
    build_news_workspace(tmp_path)
    write_app_manifest(
        tmp_path,
        "daily_digest",
        'name = "daily-digest"\ndisplay_name = "Daily digest"\nicon = "icon.svg"\n',
        is_icon_written=True,
    )
    specs = [parse_creation_spec(spec) for spec in ("app:daily-digest", "app:news", "app:daily_digest")]

    with pytest.raises(
        ScopeComputationError, match="names app:daily-digest and app:daily_digest, which are one creation"
    ):
        compute_pass_scope(tmp_path, specs, None, "HEAD", None)


def _news_lead_with_a_change(repo_root: Path) -> tuple[str, str]:
    """The news workspace on main, the commit before a chat's change to the app, and that change
    with another chat's unrelated commit after it: the diff base, and where a worker forks."""
    build_news_workspace(repo_root)
    write_repo_file(repo_root, "docs/system/unrelated.md", "# unrelated\n")
    write_repo_file(repo_root, "system/scripts/helper.sh", "#!/bin/sh\n\necho one\n\necho two\n")
    init_git_repository(repo_root)
    diff_base = commit_everything(repo_root, "base")
    commit_changes(repo_root, {"system/apps/news/runner.py": "ROUTES = ('/api/entries',)\n"}, "the chat's change")
    fork = commit_changes(repo_root, {"docs/system/unrelated.md": "# unrelated, edited\n"}, "another chat")
    return diff_base, fork


def _news_lead_with_a_merged_milestone(repo_root: Path) -> tuple[str, str, str]:
    """``_news_lead_with_a_change`` with a worker branched at the fork whose milestone, an edit
    outside the footprint, the lead provisionally merged before another chat committed again;
    the worker is checked out. The diff base, the fork, and the lead's HEAD."""
    diff_base, fork = _news_lead_with_a_change(repo_root)
    run_git(repo_root, ("checkout", "-q", "-b", "worker"))
    commit_changes(repo_root, {"system/scripts/helper.sh": "#!/bin/sh\n\necho ONE\n\necho two\n"}, "milestone")
    run_git(repo_root, ("checkout", "-q", "main"))
    run_git(repo_root, ("merge", "-q", "--no-ff", "worker", "-m", "Provisional merge of worker at milestone"))
    lead_head = commit_changes(repo_root, {"docs/system/later.md": "# later\n"}, "another chat, after the milestone")
    run_git(repo_root, ("checkout", "-q", "worker"))
    return diff_base, fork, lead_head


def test_a_milestone_the_lead_merged_does_not_move_the_passs_fork_point(tmp_path: Path) -> None:
    """The lead's merge of a provisional milestone puts the worker's commits on the lead's branch,
    but only as a second parent: the pass still forked where it did, so the edit outside the
    footprint the worker made before the milestone is still the pass's claim."""
    diff_base, fork, _ = _news_lead_with_a_merged_milestone(tmp_path)
    commit_changes(tmp_path, {"system/apps/news/runner_test.py": "def test_routes() -> None:\n    pass\n"}, "tests")

    scope = compute_pass_scope(tmp_path, [parse_creation_spec("app:news")], diff_base, "HEAD", "main")

    assert scope.diff is not None
    assert scope.diff.fork_point == fork
    assert scope.diff.inside_footprint == ("system/apps/news/runner.py", "system/apps/news/runner_test.py")
    assert scope.diff.outside_footprint == ("system/scripts/helper.sh",)
    assert scope.diff.others == ("docs/system/unrelated.md",)
    assert scope.diff.incoming_base is None


def test_a_milestone_stays_the_passs_after_the_worker_merges_the_lead_that_merged_it(tmp_path: Path) -> None:
    """Once the worker merges a lead HEAD that holds its provisionally merged milestone, the fork
    point is past the milestone's commits, but they are still on the worker's own line: the edit
    outside the footprint they made is still the pass's claim."""
    diff_base, _, lead_head = _news_lead_with_a_merged_milestone(tmp_path)
    run_git(tmp_path, ("merge", "-q", "--no-ff", "main", "-m", "Catch up"))
    commit_changes(tmp_path, {"system/apps/news/runner_test.py": "def test_routes() -> None:\n    pass\n"}, "tests")

    scope = compute_pass_scope(tmp_path, [parse_creation_spec("app:news")], diff_base, "HEAD", "main")

    assert scope.diff is not None
    assert scope.diff.fork_point == lead_head
    assert scope.diff.outside_footprint == ("system/scripts/helper.sh",)
    assert scope.diff.others == ("docs/system/later.md", "docs/system/unrelated.md")


def test_after_merging_the_leads_head_its_commits_are_the_leads_and_not_the_passs(tmp_path: Path) -> None:
    """Once a worker merges the lead's later HEAD into its branch, the pass forks from that commit:
    over the window since the worker's tip before the merge, what the merge brought into the
    creation is inside the footprint, a file both sides changed is the pass's own, and the rest
    of what came in is other work. Everything the merge brought in is incoming, from where the
    worker last held the lead's line."""
    diff_base, fork = _news_lead_with_a_change(tmp_path)
    run_git(tmp_path, ("checkout", "-q", "-b", "worker"))
    before_merge = commit_changes(
        tmp_path, {"system/scripts/helper.sh": "#!/bin/sh\n\necho ONE\n\necho two\n"}, "worker edit"
    )
    run_git(tmp_path, ("checkout", "-q", "main"))
    commit_changes(
        tmp_path,
        {
            "system/scripts/helper.sh": "#!/bin/sh\n\necho one\n\necho TWO\n",
            "docs/system/unrelated.md": "# unrelated, edited again\n",
        },
        "another chat",
    )
    lead_head = commit_changes(
        tmp_path, {"system/apps/news/runner.py": "ROUTES = ('/api/entries', '/api/feeds')\n"}, "the app moved on"
    )
    run_git(tmp_path, ("checkout", "-q", "worker"))
    run_git(tmp_path, ("merge", "-q", "--no-ff", "main", "-m", "Catch up"))

    scope = compute_pass_scope(tmp_path, [parse_creation_spec("app:news")], before_merge, "HEAD", "main")
    whole_pass = compute_pass_scope(tmp_path, [parse_creation_spec("app:news")], diff_base, "HEAD", "main")

    assert scope.diff is not None
    assert scope.diff.fork_point == lead_head
    assert scope.diff.inside_footprint == ("system/apps/news/runner.py",)
    assert scope.diff.outside_footprint == ("system/scripts/helper.sh",)
    assert scope.diff.others == ("docs/system/unrelated.md",)
    assert scope.diff.incoming_base == fork
    assert scope.diff.incoming == (
        "docs/system/unrelated.md",
        "system/apps/news/runner.py",
        "system/scripts/helper.sh",
    )
    assert whole_pass.diff is not None
    assert whole_pass.diff.outside_footprint == ("system/scripts/helper.sh",)
    assert whole_pass.diff.incoming_base == fork


def test_a_second_catch_up_brings_in_only_what_the_lead_did_since_the_first(tmp_path: Path) -> None:
    """A worker that already caught up once and catches up again starts its window at the lead
    commit the first round merged: what came in then is in the tree its last gate ran on, so only
    the second round's commits are incoming."""
    _news_lead_with_a_change(tmp_path)
    run_git(tmp_path, ("checkout", "-q", "-b", "worker"))
    commit_changes(tmp_path, {"system/scripts/helper.sh": "#!/bin/sh\n\necho ONE\n\necho two\n"}, "worker edit")
    run_git(tmp_path, ("checkout", "-q", "main"))
    first_round = commit_changes(
        tmp_path, {"docs/system/unrelated.md": "# unrelated, edited again\n"}, "another chat"
    )
    run_git(tmp_path, ("checkout", "-q", "worker"))
    run_git(tmp_path, ("merge", "-q", "--no-ff", "main", "-m", "Catch up"))
    before_second_merge = commit_changes(
        tmp_path, {"system/apps/news/runner_test.py": "def test_routes() -> None:\n    pass\n"}, "tests"
    )
    run_git(tmp_path, ("checkout", "-q", "main"))
    commit_changes(tmp_path, {"docs/system/later.md": "# later\n"}, "another chat, later")
    run_git(tmp_path, ("checkout", "-q", "worker"))
    run_git(tmp_path, ("merge", "-q", "--no-ff", "main", "-m", "Catch up again"))

    scope = compute_pass_scope(tmp_path, [parse_creation_spec("app:news")], before_second_merge, "HEAD", "main")

    assert scope.diff is not None
    assert scope.diff.incoming_base == first_round
    assert scope.diff.incoming == ("docs/system/later.md",)


def test_a_lead_ref_the_branch_never_forked_from_is_refused(tmp_path: Path) -> None:
    diff_base, _ = _news_lead_with_a_change(tmp_path)
    run_git(tmp_path, ("checkout", "-q", "--orphan", "unrelated"))
    commit_everything(tmp_path, "an unrelated history")
    run_git(tmp_path, ("checkout", "-q", "main"))

    with pytest.raises(ScopeComputationError, match="did not fork from it"):
        compute_pass_scope(tmp_path, [parse_creation_spec("app:news")], diff_base, "HEAD", "unrelated")


def test_a_file_the_hardened_change_committed_outside_every_footprint_is_the_passs(tmp_path: Path) -> None:
    """The change being hardened was committed on the lead's branch before the worker forked; a
    file it changed outside every footprint is still the pass's to test and explain. Another
    chat's commit in the same window that touched none of the pass's creations is not."""
    build_news_workspace(tmp_path)
    write_repo_file(tmp_path, "docs/system/unrelated.md", "# unrelated\n")
    write_repo_file(tmp_path, "system/scripts/helper.sh", "#!/bin/sh\n")
    write_repo_file(tmp_path, "system/scripts/other.sh", "#!/bin/sh\n")
    init_git_repository(tmp_path)
    diff_base = commit_everything(tmp_path, "base")
    commit_changes(
        tmp_path,
        {"system/apps/news/runner.py": "ROUTES = ('/api/entries',)\n", "system/scripts/helper.sh": "#!/bin/sh\necho\n"},
        "the chat's change, with a helper it needed",
    )
    commit_changes(
        tmp_path,
        {"docs/system/unrelated.md": "# unrelated, edited\n", "system/scripts/other.sh": "#!/bin/sh\necho\n"},
        "another chat",
    )
    run_git(tmp_path, ("checkout", "-q", "-b", "worker"))
    commit_changes(tmp_path, {"system/apps/news/runner_test.py": "def test_routes() -> None:\n    pass\n"}, "tests")

    scope = compute_pass_scope(tmp_path, [parse_creation_spec("app:news")], diff_base, "HEAD", "main")

    assert scope.diff is not None
    assert scope.diff.outside_footprint == ("system/scripts/helper.sh",)
    assert scope.diff.others == ("docs/system/unrelated.md", "system/scripts/other.sh")
