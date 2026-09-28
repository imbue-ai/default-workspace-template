from pathlib import Path

import pytest

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

_ALWAYS_RUN = "uv run pytest system/scripts/agent_hook_wiring_test.py system/test_layout.py"
_FULL_ROOT = "uv run pytest"
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


def test_a_consumer_change_does_not_run_what_it_consumes(workspace: Path) -> None:
    selection = _select(workspace, ["system/apps/notes/src/notes/core.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/apps/notes"]


def test_a_skill_change_runs_only_that_skills_suite(workspace: Path) -> None:
    selection = _select(workspace, [".agents/skills/refresh/scripts/refresh.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest .agents/skills/refresh"]
    assert selection.paths[0].classes == (ChangedPathClass.SKILL,)


def test_a_path_outside_every_package_and_skill_runs_the_full_root_suite(
    workspace: Path,
) -> None:
    selection = _select(workspace, ["system/scripts/forward_port.py"])

    assert _command_lines(selection) == [_FULL_ROOT]
    assert selection.is_full_root
    assert selection.paths[0].classes == (ChangedPathClass.UNOWNED,)
    assert "system/scripts/forward_port.py (full root suite)" in render_selection(selection)


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


def test_a_supervisord_block_runs_the_full_root_suite(workspace: Path) -> None:
    write_supervisord_dropin(workspace, "notes", ("program:notes",))
    commit_everything(workspace, "wire notes")

    selection = _select(workspace, ["system/supervisord.conf.d/notes.conf"])

    assert _command_lines(selection) == [_FULL_ROOT]


def test_documentation_alone_selects_nothing(workspace: Path) -> None:
    selection = _select(workspace, ["README.md", "docs/guide.md", "system/libs/corelib/README.md"])

    assert selection.commands == ()
    assert render_selection(selection) == "# every changed path is documentation, so nothing to run\n"


def test_documentation_beside_code_adds_nothing_to_what_the_code_selects(workspace: Path) -> None:
    with_docs = _select(workspace, ["README.md", "system/libs/midlib/src/midlib/core.py"])
    without_docs = _select(workspace, ["system/libs/midlib/src/midlib/core.py"])

    assert _command_lines(with_docs) == _command_lines(without_docs)


def test_every_non_documentation_change_runs_the_always_run_set(workspace: Path) -> None:
    selection = _select(workspace, ["system/test_layout.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN]
    assert selection.paths[0].classes == (ChangedPathClass.GUARD,)


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
