import datetime
import os
import subprocess
from pathlib import Path

import pytest

from onboarding_activity import markers
from onboarding_activity.markers import Marker


def test_the_markers_are_the_closed_set_the_writers_and_the_readers_share() -> None:
    assert [marker.value for marker in Marker] == [
        "tour_finished",
        "phone_layout_used",
        "second_chat_started",
        "permission_request_filed",
        "connect_data_coach_shown",
        "app_build_started",
    ]


def test_a_marker_is_unset_until_marked_and_only_that_marker_is_set(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "onboarding_activity"

    assert not markers.is_marked(Marker.TOUR_FINISHED, directory)
    assert markers.marked_at(Marker.TOUR_FINISHED, directory) is None

    markers.mark(Marker.TOUR_FINISHED, directory)

    assert markers.is_marked(Marker.TOUR_FINISHED, directory)
    assert not markers.is_marked(Marker.PHONE_LAYOUT_USED, directory)
    assert [path.name for path in directory.iterdir()] == ["tour_finished"]
    assert (directory / "tour_finished").read_bytes() == b""


def test_marked_at_is_the_files_time_in_utc_and_marking_again_refreshes_it(
    tmp_path: Path,
) -> None:
    markers.mark(Marker.APP_BUILD_STARTED, tmp_path)
    long_ago = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    os.utime(
        tmp_path / "app_build_started", (long_ago.timestamp(), long_ago.timestamp())
    )

    set_at = markers.marked_at(Marker.APP_BUILD_STARTED, tmp_path)
    assert set_at == long_ago
    assert set_at is not None and set_at.tzinfo is datetime.timezone.utc

    before = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=5
    )
    markers.mark(Marker.APP_BUILD_STARTED, tmp_path)

    refreshed = markers.marked_at(Marker.APP_BUILD_STARTED, tmp_path)
    assert refreshed is not None and refreshed > before


def test_the_default_directory_is_the_one_the_environment_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(markers.MARKER_DIR_ENV, str(tmp_path / "named"))

    markers.mark(Marker.SECOND_CHAT_STARTED)

    assert (tmp_path / "named" / "second_chat_started").is_file()
    assert markers.is_marked(Marker.SECOND_CHAT_STARTED)
    assert markers.marked_at(Marker.SECOND_CHAT_STARTED) is not None
    assert not markers.is_marked(Marker.TOUR_FINISHED)


def test_a_directory_that_cannot_be_read_is_told_apart_from_an_unset_marker(
    tmp_path: Path,
) -> None:
    # A file where the directory should be: nothing under it can be looked up or made.
    not_a_directory = tmp_path / "onboarding_activity"
    not_a_directory.write_text("")

    with pytest.raises(markers.MarkerUnreadableError):
        markers.is_marked(Marker.TOUR_FINISHED, not_a_directory)
    with pytest.raises(markers.MarkerUnreadableError):
        markers.marked_at(Marker.TOUR_FINISHED, not_a_directory)
    with pytest.raises(markers.MarkerNotWrittenError):
        markers.mark(Marker.TOUR_FINISHED, not_a_directory)


def test_the_marker_dir_is_under_the_checkout_unless_the_environment_overrides_it(
    tmp_path: Path,
) -> None:
    assert (
        markers.marker_dir({}, tmp_path)
        == tmp_path / "data" / ".state" / "onboarding_activity"
    )
    assert markers.marker_dir(
        {markers.MARKER_DIR_ENV: str(tmp_path / "elsewhere")}, tmp_path
    ) == (tmp_path / "elsewhere")
    assert markers.marker_dir({}).parts[-3:] == (
        "data",
        ".state",
        "onboarding_activity",
    )
    assert (markers.own_repo_root() / "system" / "scripts").is_dir()


def test_a_git_worktrees_marker_dir_is_its_main_checkouts(tmp_path: Path) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    git_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    for command in (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "commit", "--quiet", "--allow-empty", "-m", "initial"],
        ["git", "worktree", "add", "--quiet", str(worktree)],
    ):
        subprocess.run(command, cwd=main, env=git_env, check=True)

    assert markers.main_checkout(worktree) == main.resolve()
    assert markers.main_checkout(main) == main
    assert (
        markers.marker_dir({}, worktree)
        == main.resolve() / "data" / ".state" / "onboarding_activity"
    )
