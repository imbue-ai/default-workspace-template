from pathlib import Path

import pytest

from app_manifest.errors import RecordLoadError
from app_manifest.errors import RegistryReadError
from app_manifest.errors import SuiteSelectionError
from app_manifest.primitives import AppName
from app_manifest.records import AppOrigin
from app_manifest.records import AppRecord
from app_manifest.records import write_record
from app_manifest.testing import write_app_manifest
from app_manifest.testing import write_app_record
from app_manifest.testing import write_repo_file
from app_manifest.user_apps import has_user_built_app

_ROOT_PYPROJECT = """
[project]
name = "workspace"
version = "0.1.0"

[tool.uv.workspace]
members = ["system/apps/chat", "system/apps/files", "system/libs/*"]
"""


def _package(repo_root: Path, directory: str, name: str) -> None:
    write_repo_file(repo_root, f"{directory}/pyproject.toml", f'[project]\nname = "{name}"\nversion = "0.1.0"\n')


def _app(repo_root: Path, package: str, name: str) -> None:
    write_app_manifest(
        repo_root, package, f'name = "{name}"\ndisplay_name = "{name.title()}"\nicon = "icon.svg"\n', True
    )


def _row(name: str, flags: str = "") -> str:
    return f'[[apps]]\nname = "{name}"\nurl = "http://localhost:5000"\n{flags}'


# What a workspace nobody has built in registers: the template's own apps. The datalib app has no package and
# carries neither flag.
_TEMPLATE_ROWS = (
    _row("chat", "system = true\n")
    + _row("files")
    + _row("datalib")
    + _row("terminal-pty", "internal = true\nsystem = true\n")
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """A workspace with the template's own apps and one package its user scaffolded, ``todo-list``."""
    write_repo_file(tmp_path, "pyproject.toml", _ROOT_PYPROJECT)
    for package in ("chat", "files"):
        _package(tmp_path, f"system/apps/{package}", package)
        _app(tmp_path, package, package)
    _app(tmp_path, "datalib", "datalib")
    _package(tmp_path, "system/libs/app_manifest", "app-manifest")
    _package(tmp_path, "system/apps/todo_list", "todo-list")
    _app(tmp_path, "todo_list", "todo-list")
    return tmp_path


def _registry(repo_root: Path, rows: str) -> Path:
    return write_repo_file(repo_root, "data/.state/apps.toml", rows)


def test_a_workspace_holding_only_the_templates_own_apps_has_no_user_built_app(repo_root: Path) -> None:
    # The scaffolded package is on disk, but nothing has registered it.
    assert not has_user_built_app(repo_root, _registry(repo_root, _TEMPLATE_ROWS))
    assert not has_user_built_app(repo_root, _registry(repo_root, ""))
    assert not has_user_built_app(repo_root, repo_root / "data" / ".state" / "missing.toml")


def test_a_registered_user_package_is_a_user_built_app(repo_root: Path) -> None:
    assert has_user_built_app(repo_root, _registry(repo_root, _TEMPLATE_ROWS + _row("todo-list")))
    assert has_user_built_app(repo_root, _registry(repo_root, _row("todo-list")))


def test_an_app_still_framed_as_a_mock_does_not_count(repo_root: Path) -> None:
    # While the mock frame is up the app's own row is internal, and the frame's row belongs to no package.
    framed = _TEMPLATE_ROWS + _row("todo-list", "internal = true\n") + _row("todo-list-sketch")

    assert not has_user_built_app(repo_root, _registry(repo_root, framed))


def test_a_preview_does_not_count(repo_root: Path) -> None:
    # A preview of a change registers the framed copy as internal and the frame under a name of its own.
    previews = (
        _TEMPLATE_ROWS
        + _row("files-preview-app", "internal = true\n")
        + _row("files-preview")
        + _row("todo-list-preview-app", "internal = true\n")
        + _row("todo-list-preview")
    )

    assert not has_user_built_app(repo_root, _registry(repo_root, previews))


def test_a_user_app_flagged_system_or_internal_does_not_count(repo_root: Path) -> None:
    assert not has_user_built_app(repo_root, _registry(repo_root, _row("todo-list", "system = true\n")))
    assert not has_user_built_app(repo_root, _registry(repo_root, _row("todo-list", "internal = true\n")))


def test_only_a_package_under_the_apps_directory_with_a_manifest_that_loads_counts(repo_root: Path) -> None:
    # A user's service is a package the root does not list, but it is no app.
    _package(repo_root, "system/services/mail_sync", "mail-sync")
    # A package with no manifest, and one whose manifest names an icon that is not there.
    _package(repo_root, "system/apps/bare", "bare")
    _package(repo_root, "system/apps/broken", "broken")
    write_app_manifest(repo_root, "broken", 'name = "broken"\ndisplay_name = "Broken"\nicon = "icon.svg"\n', False)
    rows = _TEMPLATE_ROWS + _row("mail-sync") + _row("bare") + _row("broken")

    assert not has_user_built_app(repo_root, _registry(repo_root, rows))


def test_an_app_counts_under_its_manifests_name_not_its_directorys(repo_root: Path) -> None:
    _package(repo_root, "system/apps/notes_pkg", "notes-pkg")
    _app(repo_root, "notes_pkg", "notes")

    assert has_user_built_app(repo_root, _registry(repo_root, _TEMPLATE_ROWS + _row("notes")))
    assert not has_user_built_app(repo_root, _registry(repo_root, _TEMPLATE_ROWS + _row("notes-pkg")))


def test_an_app_installed_from_someone_elses_repository_is_not_one_the_user_built(repo_root: Path) -> None:
    _package(repo_root, "system/apps/notes", "notes")
    _app(repo_root, "notes", "notes")
    write_record(
        repo_root,
        AppRecord(
            name=AppName("notes"),
            package="notes",
            port=8090,
            origin=AppOrigin.INSTALLED,
            upstream_url="https://github.com/someone/notes",
        ),
    )

    assert not has_user_built_app(repo_root, _registry(repo_root, _TEMPLATE_ROWS + _row("notes")))

    # An app the workspace built has a record too, and still counts beside an installed one.
    write_app_record(repo_root, "todo-list", "todo_list", {})

    assert has_user_built_app(repo_root, _registry(repo_root, _TEMPLATE_ROWS + _row("notes") + _row("todo-list")))


def test_an_app_record_that_cannot_be_read_raises(repo_root: Path) -> None:
    write_repo_file(repo_root, "system/config/apps/notes.toml", "name = \n")

    with pytest.raises(RecordLoadError):
        has_user_built_app(repo_root, _registry(repo_root, _row("todo-list")))


def test_a_registry_or_a_root_project_that_cannot_be_read_raises(repo_root: Path) -> None:
    with pytest.raises(RegistryReadError):
        has_user_built_app(repo_root, _registry(repo_root, "[[apps]\nname = \n"))
    registry = _registry(repo_root, _row("todo-list"))
    (repo_root / "pyproject.toml").write_text("[project\n")
    with pytest.raises(SuiteSelectionError):
        has_user_built_app(repo_root, registry)
