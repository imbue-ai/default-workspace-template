from pathlib import Path

import pytest

from app_manifest.errors import SuiteSelectionError
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

_ALWAYS_RUN = "uv run pytest system/scripts/hook_wiring_test.py system/test_layout.py"
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
        # The skill's script imports corelib without a pyproject.toml to say so.
        "uv run pytest .agents/skills/refresh",
        # notes reaches corelib only through midlib.
        "uv run pytest system/apps/notes",
        "uv run pytest system/libs/corelib",
        "uv run pytest system/libs/midlib",
        # Reached through a library, the chat app runs without its browser tests.
        _CHAT_WHOLE_WITHOUT_BROWSER,
        _CHAT_TYPE_CHECK,
    ]
    assert not selection.is_full_root


def test_a_shared_library_change_runs_the_tests_of_scripts_importing_a_consumer(
    workspace: Path,
) -> None:
    write_repo_file(workspace, "system/scripts/mid_report.py", "from midlib.core import VALUE\n")
    write_repo_file(
        workspace, "system/scripts/mid_report_test.py", "def test_report() -> None:\n    pass\n"
    )
    commit_everything(workspace, "a script that imports midlib")

    selection = _select(workspace, ["system/libs/corelib/src/corelib/core.py"])

    assert "uv run pytest system/scripts/mid_report_test.py" in _command_lines(selection)


def test_a_consumer_change_does_not_run_what_it_consumes(workspace: Path) -> None:
    selection = _select(workspace, ["system/apps/notes/src/notes/core.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/apps/notes"]


def test_a_script_runs_the_test_paired_with_it_by_filename_and_no_other(workspace: Path) -> None:
    selection = _select(workspace, ["system/scripts/forward_port.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/scripts/forward_port_test.py"]


def test_a_script_under_its_own_directory_pairs_with_the_test_there(workspace: Path) -> None:
    selection = _select(workspace, ["system/scripts/agy_shim/agy_shim.sh"])

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest system/scripts/agy_shim/agy_shim_test.py",
    ]


def test_a_test_prefixed_file_pairs_with_the_script_it_names(workspace: Path) -> None:
    selection = _select(workspace, ["system/scripts/create_gate.py"])

    assert "uv run pytest system/scripts/test_create_gate.py" in _command_lines(selection)


def test_a_new_script_with_no_pair_is_unclassified_and_brings_in_the_full_root_suite(
    workspace: Path,
) -> None:
    write_repo_file(workspace, "system/scripts/brand_new.py", "X = 1\n")
    commit_everything(workspace, "add a script")

    selection = _select(workspace, ["system/scripts/brand_new.py"])

    assert _command_lines(selection) == ["uv run pytest"]
    assert selection.unclassified == ("system/scripts/brand_new.py",)
    assert selection.is_full_root
    rendered = render_selection(selection)
    assert "#   system/scripts/brand_new.py" in rendered
    assert "test_selection_overrides.toml" in rendered


def test_a_deleted_script_and_its_test_are_classified_without_the_full_root_suite(
    workspace: Path,
) -> None:
    write_repo_file(
        workspace,
        "system/scripts/gate_callers_test.py",
        "# Runs system/scripts/create_gate.py.\ndef test_callers() -> None:\n    pass\n",
    )
    commit_everything(workspace, "a test that names the gate")
    (workspace / "system/scripts/create_gate.py").unlink()
    (workspace / "system/scripts/test_create_gate.py").unlink()
    commit_everything(workspace, "retire the gate")

    selection = _select(
        workspace, ["system/scripts/create_gate.py", "system/scripts/test_create_gate.py"]
    )

    assert not selection.is_full_root
    assert selection.unclassified == ()
    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest system/scripts/gate_callers_test.py",
    ]


def test_the_full_root_suite_replaces_the_root_collected_runs_but_not_the_own_root_suites(
    workspace: Path,
) -> None:
    selection = _select(workspace, ["unknown.bin", "system/apps/chat/imbue/chat/server.py"])

    lines = _command_lines(selection)
    assert "uv run pytest" in lines
    assert _ALWAYS_RUN not in lines
    assert _CHAT_WHOLE_WITH_BROWSER in lines


def test_a_file_a_test_names_runs_that_test(workspace: Path) -> None:
    selection = _select(workspace, ["system/scripts/banner.txt"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/scripts/banner_test.py"]
    assert selection.paths[0].classes == (ChangedPathClass.NAMED_BY_TEST,)


def test_a_helper_module_a_test_imports_runs_that_test(workspace: Path) -> None:
    selection = _select(workspace, ["system/scripts/shape_testing.py"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/scripts/shape_test.py"]


def test_documentation_alone_selects_nothing(workspace: Path) -> None:
    selection = _select(workspace, ["README.md", "docs/guide.md", "system/libs/corelib/README.md"])

    assert selection.commands == ()
    assert render_selection(selection) == "# every changed path is documentation, so nothing to run\n"


def test_documentation_beside_code_adds_nothing_to_what_the_code_selects(workspace: Path) -> None:
    with_docs = _select(workspace, ["README.md", "system/scripts/forward_port.py"])
    without_docs = _select(workspace, ["system/scripts/forward_port.py"])

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


def test_a_path_an_app_manifest_references_runs_that_app(workspace: Path) -> None:
    selection = _select(workspace, ["system/scripts/run_notes.sh"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/apps/notes"]
    assert selection.paths[0].classes == (ChangedPathClass.MANIFEST_REFERENCE,)


def test_an_app_change_runs_the_tests_of_the_directories_its_manifest_references(
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

    selection = _select(workspace, ["system/apps/notes/src/notes/core.py"])

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest .agents/skills/refresh",
        "uv run pytest system/apps/notes",
    ]


def test_a_supervisord_block_runs_the_app_it_starts(workspace: Path) -> None:
    write_supervisord_dropin(workspace, "notes", ("program:notes",))
    commit_everything(workspace, "wire notes")

    selection = _select(workspace, ["system/supervisord.conf.d/notes.conf"])

    assert _command_lines(selection) == [_ALWAYS_RUN, "uv run pytest system/apps/notes"]


def test_the_override_file_selects_consumers_and_integration_tests(workspace: Path) -> None:
    selection = _select(workspace, ["catalog/templates.json", ".mngr/settings.toml"])

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        "uv run pytest system/apps/notes",
        "uv run pytest system/scripts/test_create_gate.py",
    ]


def test_an_override_naming_a_suite_that_does_not_exist_fails_loudly(workspace: Path) -> None:
    write_repo_file(
        workspace,
        "system/libs/app_manifest/src/app_manifest/test_selection_overrides.toml",
        'always_run = []\n\n[[consumer]]\npaths = ["catalog/**"]\nsuites = ["system/apps/gone"]\nnote = "stale"\n\n',
    )
    commit_everything(workspace, "a stale override")

    with pytest.raises(SuiteSelectionError, match="system/apps/gone"):
        _select(workspace, ["catalog/templates.json"])


def test_an_upgraded_lock_entry_runs_the_members_that_depend_on_it(workspace: Path) -> None:
    base_lock = selection_lock("2.0")
    head_lock = selection_lock("2.1")

    selection = _select(workspace, ["uv.lock"], (base_lock, head_lock))

    assert _command_lines(selection) == [
        _ALWAYS_RUN,
        # The skill's script imports corelib, which depends on the upgrade.
        "uv run pytest .agents/skills/refresh",
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

    assert _command_lines(selection) == ["uv run pytest"]
    assert any("cannot parse the base uv.lock" in note for note in selection.notes)


def test_a_lock_with_no_base_to_compare_brings_in_the_full_root_suite(workspace: Path) -> None:
    selection = _select(workspace, ["uv.lock"], None)

    assert _command_lines(selection) == ["uv run pytest"]
    assert selection.paths[0].classes == (ChangedPathClass.LOCKFILE,)
