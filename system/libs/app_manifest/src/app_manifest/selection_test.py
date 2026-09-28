from pathlib import Path

import pytest

from app_manifest.errors import SuiteSelectionError
from app_manifest.selection import ALWAYS_RUN_GUARDS
from app_manifest.selection import ChangedPathClass
from app_manifest.selection import SuiteSelection
from app_manifest.selection import load_repo_layout
from app_manifest.selection import render_command_line
from app_manifest.selection import render_selection
from app_manifest.selection import select_tests
from app_manifest.testing import build_selection_workspace
from app_manifest.testing import commit_everything
from app_manifest.testing import selection_lock
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_repo_file
from app_manifest.testing import write_supervisord_dropin

_ALWAYS_RUN = " ".join(
    ("uv", "run", "pytest", *sorted({*ALWAYS_RUN_GUARDS, "system/test_layout.py"}))
)
_FULL_ROOT = "uv run pytest"
_BAND_CHECK = "uv run pytest system/services/oom_priority/bin/oom_tag_service_test.py"
_CHAT_WHOLE_WITHOUT_BROWSER = (
    "(cd system/apps/chat && uv run pytest --deselect imbue/chat/test_ratchets.py::test_no_type_errors)"
)
_CHAT_WHOLE_WITH_BROWSER = (
    "(cd system/apps/chat && uv run pytest -m '' --deselect imbue/chat/test_ratchets.py::test_no_type_errors)"
)
_CHAT_TYPE_CHECK = "(cd system/apps/chat && uv run ty check)"
_FRONTEND_BUILD = ["(cd system && npm ci)", "(cd system && npm run build)"]


def _select(
    repo_root: Path, paths: list[str], lockfile_texts: tuple[str, str] | None = None
) -> SuiteSelection:
    return select_tests(load_repo_layout(repo_root), paths, lockfile_texts)


def _command_lines(selection: SuiteSelection) -> list[str]:
    return [render_command_line(command) for command in selection.commands]


def _declare_root_dependency(repo_root: Path, name: str) -> None:
    root_pyproject = repo_root / "pyproject.toml"
    root_pyproject.write_text(
        root_pyproject.read_text().replace(
            'version = "0.1.0"\n', f'version = "0.1.0"\ndependencies = ["{name}"]\n', 1
        )
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    build_selection_workspace(tmp_path)
    return tmp_path


def test_a_shared_library_change_runs_its_suite_and_every_transitive_consumer(
    workspace: Path,
) -> None:
    selection = _select(workspace, ["system/libs/corelib/src/corelib/core.py"])

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        # notes reaches corelib only through midlib.
        "uv run pytest system/apps/notes",
        "uv run pytest system/libs/corelib",
        "uv run pytest system/libs/midlib",
        # Reached through a library, the chat app runs without its browser tests.
        _CHAT_WHOLE_WITHOUT_BROWSER,
        _CHAT_TYPE_CHECK,
    ]
    assert not selection.is_full_root


def test_a_shared_library_test_change_runs_only_the_librarys_own_suite(workspace: Path) -> None:
    selection = _select(workspace, ["system/libs/corelib/src/corelib/core_test.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/libs/corelib"]


@pytest.mark.parametrize(
    "path",
    [
        "system/libs/midlib/src/midlib/core.py",
        # Reaches the root project through midlib, which depends on it.
        "system/libs/corelib/src/corelib/core.py",
    ],
)
def test_a_package_the_root_project_depends_on_runs_the_full_root_suite(
    workspace: Path, path: str
) -> None:
    _declare_root_dependency(workspace, "midlib")

    selection = _select(workspace, [path])

    assert selection.is_full_root
    assert _FULL_ROOT in _command_lines(selection)
    assert f"{path} (full root suite)" in render_selection(selection)
    # A test file is run by nothing that depends on its package.
    test_only = _select(workspace, ["system/libs/midlib/src/midlib/core_test.py"])
    assert _command_lines(test_only) == [_ALWAYS_RUN, "uv run pytest system/libs/midlib"]


def test_a_consumer_change_does_not_run_what_it_consumes(workspace: Path) -> None:
    selection = _select(workspace, ["system/apps/notes/src/notes/core.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/apps/notes"]


def test_a_skill_change_runs_only_that_skills_suite(workspace: Path) -> None:
    selection = _select(workspace, [".agents/skills/refresh/scripts/refresh.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest .agents/skills/refresh"]
    assert selection.paths[0].classes == (ChangedPathClass.SKILL,)


@pytest.mark.parametrize(
    "path",
    [
        "system/scripts/forward_port.py",
        # Beside the guards but not one: it applies to every root-collected test under system/.
        "system/conftest.py",
    ],
)
def test_a_path_outside_every_package_and_skill_runs_the_full_root_suite(
    workspace: Path, path: str
) -> None:
    selection = _select(workspace, [path])

    assert _command_lines(selection) == [_FULL_ROOT]
    assert selection.is_full_root
    assert selection.paths[0].classes == (ChangedPathClass.UNOWNED,)
    assert f"{path} (full root suite)" in render_selection(selection)


def test_a_changed_test_file_outside_every_package_runs_only_itself(workspace: Path) -> None:
    selection = _select(workspace, ["system/scripts/forward_port_test.py"])

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest system/scripts/forward_port_test.py",
    ]


def test_the_full_root_suite_replaces_the_root_collected_runs_but_not_the_own_root_suites(
    workspace: Path,
) -> None:
    selection = _select(
        workspace,
        ["unknown.bin", "system/libs/midlib/src/midlib/core.py", "system/apps/chat/imbue/chat/server.py"],
    )

    lines = _command_lines(selection)
    assert _FULL_ROOT in lines
    assert _ALWAYS_RUN not in lines
    assert "uv run pytest system/libs/midlib" not in lines
    assert _CHAT_WHOLE_WITH_BROWSER in lines


def test_a_supervisord_block_runs_the_app_it_starts_and_the_band_check(workspace: Path) -> None:
    write_supervisord_dropin(workspace, "notes", ("program:notes",))
    commit_everything(workspace, "wire notes")

    selection = _select(workspace, ["system/supervisord.conf.d/notes.conf"])

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest system/apps/notes",
        _BAND_CHECK,
    ]
    assert selection.paths[0].classes == (ChangedPathClass.WIRING,)


def test_a_supervisord_block_no_app_owns_runs_the_band_check(workspace: Path) -> None:
    # A service's block: no manifest claims it, but it still has to name its OOM band.
    write_supervisord_dropin(workspace, "fetcher", ("program:fetcher",))
    commit_everything(workspace, "wire a service")

    selection = _select(workspace, ["system/supervisord.conf.d/fetcher.conf"])

    assert _command_lines(selection) == [_ALWAYS_RUN, _BAND_CHECK]
    assert not selection.is_full_root


def test_a_named_test_file_inside_a_selected_whole_suite_runs_only_there(workspace: Path) -> None:
    write_supervisord_dropin(workspace, "fetcher", ("program:fetcher",))
    write_repo_file(workspace, "system/services/oom_priority/bin/oom_tag_service.py", "BANDS = ()\n")
    commit_everything(workspace, "wire a service")

    selection = _select(
        workspace,
        ["system/supervisord.conf.d/fetcher.conf", "system/services/oom_priority/bin/oom_tag_service.py"],
    )

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/services/oom_priority"]


def test_documentation_alone_selects_nothing(workspace: Path) -> None:
    selection = _select(
        workspace,
        [
            "system/libs/corelib/README.md",
            "docs/system/blueprint/plan.md",
            "system/changelog/work.md",
            ".agents/changelog/work.md",
        ],
    )

    assert selection.commands == ()
    assert render_selection(selection) == "# every changed path is documentation, so nothing to run\n"


def test_prose_the_always_run_checks_read_runs_the_always_run_set(workspace: Path) -> None:
    # The live-prose terminology ratchets read the root prose and docs/, so an edit to them
    # alone can fail an always-run guard.
    selection = _select(workspace, ["AGENTS.md", "README.md", "docs/guide.md"])

    assert _command_lines(selection) == [_ALWAYS_RUN]
    assert {entry.classes for entry in selection.paths} == {(ChangedPathClass.GUARD,)}


def test_documentation_beside_code_adds_nothing_to_what_the_code_selects(workspace: Path) -> None:
    with_docs = _select(
        workspace, ["system/libs/corelib/README.md", "system/libs/midlib/src/midlib/core.py"]
    )
    without_docs = _select(workspace, ["system/libs/midlib/src/midlib/core.py"])

    assert _command_lines(with_docs) == _command_lines(without_docs)


def test_every_non_documentation_change_runs_the_always_run_set(workspace: Path) -> None:
    selection = _select(workspace, ["system/test_layout.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN]
    assert selection.paths[0].classes == (ChangedPathClass.GUARD,)


@pytest.mark.parametrize(
    ("guard", "changed_path"),
    [
        ("system/scripts/provision_guard_test.py", "system/test_layout.py"),
        ("system/services/oom_priority/bin/oom_tag_service_test.py", "system/supervisord.conf.d/fetcher.conf"),
    ],
    ids=["always-run", "wiring"],
)
def test_a_listed_guard_git_does_not_track_fails_the_selection(
    workspace: Path, guard: str, changed_path: str
) -> None:
    (workspace / guard).unlink()
    commit_everything(workspace, "drop a guard")

    with pytest.raises(SuiteSelectionError, match=guard):
        _select(workspace, [changed_path])


def test_agent_prose_outside_every_skill_runs_only_the_always_run_set(workspace: Path) -> None:
    write_repo_file(workspace, ".agents/shared/references/dispatch.md", "# dispatch\n")
    write_repo_file(workspace, ".agents/shared/scripts/dispatch.py", "X = 1\n")
    commit_everything(workspace, "shared agent prose and a shared script")

    prose = _select(workspace, [".agents/shared/references/dispatch.md"])
    script = _select(workspace, [".agents/shared/scripts/dispatch.py"])

    assert _command_lines(prose) == [_ALWAYS_RUN]
    assert _command_lines(script) == [_FULL_ROOT]


def test_agent_prose_an_app_manifest_references_runs_that_app(workspace: Path) -> None:
    write_repo_file(workspace, ".agents/shared/references/notes-guide.md", "# guide\n")
    write_app_manifest(
        workspace,
        "notes",
        'name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\n\n'
        '[[references]]\npath = ".agents/shared/references/notes-guide.md"\n',
        is_icon_written=True,
    )
    commit_everything(workspace, "notes references a shared guide")

    selection = _select(workspace, [".agents/shared/references/notes-guide.md"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/apps/notes"]
    assert selection.paths[0].classes == (
        ChangedPathClass.GUARD,
        ChangedPathClass.MANIFEST_REFERENCE,
    )


def test_a_shared_frontend_library_change_builds_then_runs_consumer_checks_and_browser_tests(
    workspace: Path,
) -> None:
    selection = _select(workspace, ["system/libs/ui/src/index.ts"])

    assert _command_lines(selection) == [
        *_FRONTEND_BUILD,
        "(cd system && npm test --workspace=apps/chat/frontend --workspace=libs/ui)",
        "(cd system && npm run lint --workspace=apps/chat/frontend --workspace=libs/ui)",
        "(cd system && npm run format:check --workspace=apps/chat/frontend --workspace=libs/ui)",
        # The library has no build, so the build does not type-check it.
        "(cd system && npm run typecheck --workspace=libs/ui)",
        _ALWAYS_RUN,
        # Only the chat app's browser tests can observe the library; the rest of its suite cannot.
        "(cd system/apps/chat && uv run pytest --no-cov -m '' imbue/chat/test_e2e.py)",
    ]


def test_the_npm_roots_prebuild_script_runs_every_frontends_checks_instead_of_the_full_root_suite(
    workspace: Path,
) -> None:
    selection = _select(workspace, ["system/scripts/fetch_mngr_assets.sh"])

    assert _command_lines(selection) == [
        *_FRONTEND_BUILD,
        "(cd system && npm test --workspace=apps/chat/frontend --workspace=libs/ui)",
        "(cd system && npm run lint --workspace=apps/chat/frontend --workspace=libs/ui)",
        "(cd system && npm run format:check --workspace=apps/chat/frontend --workspace=libs/ui)",
        "(cd system && npm run typecheck --workspace=libs/ui)",
        _ALWAYS_RUN,
        "(cd system/apps/chat && uv run pytest --no-cov -m '' imbue/chat/test_e2e.py)",
    ]


def test_a_backend_change_to_the_app_runs_its_browser_tests_and_splits_out_its_type_check(
    workspace: Path,
) -> None:
    selection = _select(workspace, ["system/apps/chat/imbue/chat/server.py"])

    assert _command_lines(selection) == [
        # The browser tests skip when the bundles are missing, so they are built first.
        *_FRONTEND_BUILD,
        _ALWAYS_RUN,
        _CHAT_WHOLE_WITH_BROWSER,
        _CHAT_TYPE_CHECK,
    ]


def test_a_frontend_change_to_the_app_runs_its_npm_checks_and_its_whole_suite(
    workspace: Path,
) -> None:
    selection = _select(workspace, ["system/apps/chat/frontend/src/main.ts"])

    assert _command_lines(selection) == [
        *_FRONTEND_BUILD,
        "(cd system && npm test --workspace=apps/chat/frontend)",
        "(cd system && npm run lint --workspace=apps/chat/frontend)",
        "(cd system && npm run format:check --workspace=apps/chat/frontend)",
        _ALWAYS_RUN,
        _CHAT_WHOLE_WITH_BROWSER,
        _CHAT_TYPE_CHECK,
    ]


def test_a_frontend_change_does_not_reach_the_python_consumers_of_the_apps_package(
    workspace: Path,
) -> None:
    write_repo_file(
        workspace,
        "system/apps/shelf/pyproject.toml",
        '[project]\nname = "shelf"\ndependencies = ["chat"]\n\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/shelf"]\n',
    )
    write_repo_file(workspace, "system/apps/shelf/src/shelf/__init__.py", "")
    write_repo_file(
        workspace, "system/apps/shelf/src/shelf/shelf_test.py", "def test_shelf() -> None:\n    pass\n"
    )
    commit_everything(workspace, "an app that depends on the chat app")

    frontend = _select(workspace, ["system/apps/chat/frontend/src/main.ts"])
    backend = _select(workspace, ["system/apps/chat/imbue/chat/server.py"])

    assert "uv run pytest system/apps/shelf" not in _command_lines(frontend)
    assert _CHAT_WHOLE_WITH_BROWSER in _command_lines(frontend)
    assert "uv run pytest system/apps/shelf" in _command_lines(backend)


def test_a_path_an_app_manifest_references_runs_that_app_and_the_references_tests(
    workspace: Path,
) -> None:
    write_app_manifest(
        workspace,
        "notes",
        'name = "notes"\ndisplay_name = "Notes"\nicon = "icon.svg"\n\n'
        '[[references]]\npath = ".agents/skills/refresh"\n',
        is_icon_written=True,
    )
    commit_everything(workspace, "notes references the refresh skill")

    from_reference = _select(workspace, [".agents/skills/refresh/scripts/refresh.py"])
    from_app = _select(workspace, ["system/apps/notes/src/notes/core.py"])

    expected = [
        _ALWAYS_RUN,
        "uv run pytest .agents/skills/refresh",
        "uv run pytest system/apps/notes",
    ]
    assert _command_lines(from_reference) == expected
    assert from_reference.paths[0].classes == (
        ChangedPathClass.SKILL,
        ChangedPathClass.MANIFEST_REFERENCE,
    )
    assert _command_lines(from_app) == expected


def test_an_upgraded_lock_entry_runs_the_members_that_depend_on_it(workspace: Path) -> None:
    selection = _select(workspace, ["uv.lock"], (selection_lock("2.0"), selection_lock("2.1")))

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest system/apps/notes",
        "uv run pytest system/libs/corelib",
        "uv run pytest system/libs/midlib",
        _CHAT_WHOLE_WITHOUT_BROWSER,
        _CHAT_TYPE_CHECK,
    ]


def test_an_upgrade_reaching_a_package_the_root_project_depends_on_runs_the_full_root_suite(
    workspace: Path,
) -> None:
    # requests reaches midlib through corelib.
    _declare_root_dependency(workspace, "midlib")

    selection = _select(workspace, ["uv.lock"], (selection_lock("2.0"), selection_lock("2.1")))

    assert selection.is_full_root
    assert _command_lines(selection) == [_FULL_ROOT, _CHAT_WHOLE_WITHOUT_BROWSER, _CHAT_TYPE_CHECK]


def test_a_lock_that_only_adds_packages_runs_nothing_beyond_the_adder(workspace: Path) -> None:
    base_lock = selection_lock("2.0")
    head_lock = base_lock + '\n[[package]]\nname = "newdep"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'

    selection = _select(workspace, ["uv.lock", "system/libs/midlib/pyproject.toml"], (base_lock, head_lock))

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest system/apps/notes",
        "uv run pytest system/libs/midlib",
    ]


def test_a_lock_that_does_not_parse_brings_in_the_full_root_suite(workspace: Path) -> None:
    selection = _select(workspace, ["uv.lock"], ("not [ toml", selection_lock("2.0")))

    assert _command_lines(selection) == [_FULL_ROOT]
    assert any("cannot parse the base uv.lock" in note for note in selection.notes)


def test_the_full_root_suite_names_only_the_paths_that_brought_it_in(workspace: Path) -> None:
    selection = _select(
        workspace, ["uv.lock", "unknown.bin"], (selection_lock("2.0"), selection_lock("2.1"))
    )

    full_root = next(command for command in selection.commands if command.argv == ("uv", "run", "pytest"))
    # The lock's upgrade reached only its dependents; the unknown file is why the full root runs.
    assert [reason.path for reason in full_root.reasons] == ["unknown.bin"]
    assert _CHAT_WHOLE_WITHOUT_BROWSER in _command_lines(selection)


def test_a_lock_with_no_base_to_compare_brings_in_the_full_root_suite(workspace: Path) -> None:
    selection = _select(workspace, ["uv.lock"], None)

    assert _command_lines(selection) == [_FULL_ROOT]
    assert selection.paths[0].classes == (ChangedPathClass.LOCKFILE,)


def test_an_ignored_vendored_subtree_selects_none_of_its_tests(workspace: Path) -> None:
    write_repo_file(
        workspace,
        "pyproject.toml",
        (workspace / "pyproject.toml").read_text().replace(
            '"--ignore=system/apps/chat"', '"--ignore=system/apps/chat", "--ignore=system/vendor/tool"'
        ),
    )
    write_repo_file(
        workspace, "system/vendor/tool/pyproject.toml", '[tool.pytest.ini_options]\naddopts = ["-q"]\n'
    )
    write_repo_file(workspace, "system/vendor/tool/tool_test.py", "import corelib\n")
    commit_everything(workspace, "vendor a tool")

    selection = _select(workspace, ["system/vendor/tool/tool_test.py"])

    assert not any("system/vendor/tool" in line for line in _command_lines(selection))
