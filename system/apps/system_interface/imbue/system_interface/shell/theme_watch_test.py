"""Tests for the theme watch: every window is told when the workspace's theme catalog changes, and only then."""

from pathlib import Path

import pytest
from workspace_themes.catalog import CachingThemeCatalogLoader
from workspace_themes.contract import WORKSPACE_THEMES_DIRECTORY
from workspace_themes.testing import write_standard_theme
from workspace_themes.testing import write_test_theme

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.shell.theme_watch import ThemeCatalogWatch


def _watch(repo_root: Path, changes: list[int]) -> ThemeCatalogWatch:
    write_standard_theme(repo_root)
    return ThemeCatalogWatch(
        repo_root=repo_root,
        loader=CachingThemeCatalogLoader(repo_root=repo_root),
        on_catalog_changed=lambda: changes.append(len(changes)),
    )


def test_a_check_tells_the_windows_only_when_the_catalog_changed(tmp_path: Path) -> None:
    changes: list[int] = []
    watch = _watch(tmp_path, changes)
    watch.start()
    try:
        watch.check_now()
        assert changes == []

        write_test_theme(tmp_path / WORKSPACE_THEMES_DIRECTORY, "paper", "")
        watch.check_now()
        watch.check_now()

        assert changes == [0]
    finally:
        watch.stop()


@pytest.mark.timeout(30)
def test_a_theme_root_made_while_the_watch_runs_is_watched_from_then_on(tmp_path: Path) -> None:
    changes: list[int] = []
    watch = _watch(tmp_path, changes)
    watch.start()
    try:
        folder = write_test_theme(tmp_path / WORKSPACE_THEMES_DIRECTORY, "paper", "")
        wait_for(lambda: len(changes) == 1, timeout=10.0)

        (folder / "parts.css").write_text('[data-part="window"] { color: #000000; }\n', encoding="utf-8")
        wait_for(lambda: len(changes) == 2, timeout=10.0)
    finally:
        watch.stop()


def _write_chat_overlay_theme(repo_root: Path) -> None:
    """A workspace theme whose chat overlay styles ``chat.user-message``."""
    folder = write_test_theme(repo_root / WORKSPACE_THEMES_DIRECTORY, "paper", "")
    manifest_path = folder / "theme.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            'files = ["parts.css"]', 'files = ["parts.css"]\napps = ["chat"]'
        ),
        encoding="utf-8",
    )
    (folder / "apps").mkdir()
    (folder / "apps" / "chat.css").write_text('[data-part="chat.user-message"] { color: #000000; }\n', "utf-8")


def _write_chat_manifest(repo_root: Path, part_name: str) -> None:
    app_folder = repo_root / "system" / "apps" / "chat"
    app_folder.mkdir(parents=True, exist_ok=True)
    (app_folder / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>\n', "utf-8")
    (app_folder / "app.toml").write_text(
        'name = "chat"\ndisplay_name = "Chat"\nicon = "icon.svg"\n\n[theming]\nmode = "parts"\n'
        f'parts = [{{ name = "{part_name}", description = "A message the user sent" }}]\n',
        encoding="utf-8",
    )


@pytest.mark.timeout(30)
def test_an_app_manifest_that_changes_a_theme_s_availability_tells_the_windows(tmp_path: Path) -> None:
    changes: list[int] = []
    watch = _watch(tmp_path, changes)
    _write_chat_overlay_theme(tmp_path)
    (tmp_path / "system" / "apps").mkdir(parents=True)
    loader = CachingThemeCatalogLoader(repo_root=tmp_path)
    watch.start()
    try:
        # The app arrives declaring a part other than the one the overlay styles: the theme becomes unavailable.
        _write_chat_manifest(tmp_path, "composer")
        wait_for(lambda: len(changes) == 1, timeout=10.0)
        entry = loader.load().find("paper")
        assert entry is not None and not entry.is_available

        # The app renames its part to the one the overlay styles: the theme is available again.
        _write_chat_manifest(tmp_path, "user-message")
        wait_for(lambda: len(changes) == 2, timeout=10.0)
        entry = loader.load().find("paper")
        assert entry is not None and entry.is_available
    finally:
        watch.stop()


def test_a_stopped_watch_tells_no_window_about_a_change_it_is_handed(tmp_path: Path) -> None:
    changes: list[int] = []
    watch = _watch(tmp_path, changes)
    watch.start()
    watch.stop()

    write_test_theme(tmp_path / WORKSPACE_THEMES_DIRECTORY, "paper", "")
    # What an event the observer delivered while stopping would start, and what its settle timer would then run.
    watch.schedule_check()
    watch.check_now()

    assert changes == []
