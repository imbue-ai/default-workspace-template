from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from versioning.data_types import CommitRecord
from versioning.data_types import VersionKind
from versioning.history import build_version_nodes
from versioning.history import discover_apps
from versioning.history import relative_time_label
from versioning.history import short_relative_time_label
from versioning.trailers import parse_trailer_block

_BASE_TIME = datetime(2026, 8, 26, 18, 0, 0, tzinfo=timezone.utc)


def _commit(sha: str, subject: str, minutes: int, message_body: str = "") -> CommitRecord:
    full_message = subject + ("\n\n" + message_body if message_body else "")
    return CommitRecord(
        sha=sha,
        author="Chat-2",
        authored_at=_BASE_TIME + timedelta(minutes=minutes),
        subject=subject,
        body=message_body,
        trailers=parse_trailer_block(full_message),
    )


def test_two_commits_by_same_author_minutes_apart_are_two_nodes() -> None:
    commits = [
        _commit("a" * 40, "science-explorer: a visual science encyclopedia", 0),
        _commit("b" * 40, "science-explorer: put the world behind an interface", 11),
    ]

    nodes = build_version_nodes(commits)

    assert len(nodes) == 2
    assert nodes[0].sha == "a" * 40
    assert nodes[1].sha == "b" * 40
    assert nodes[1].is_current
    assert not nodes[0].is_current


def test_linear_history_chains_parents_in_order() -> None:
    commits = [_commit(str(i) * 40, f"change {i}", i * 60) for i in range(1, 4)]

    nodes = build_version_nodes(commits)

    assert [n.parent_sha for n in nodes] == [None, "1" * 40, "2" * 40]
    assert all(not n.is_set_aside for n in nodes)


def test_restore_trailer_redirects_parent_and_sets_aside_skipped_versions() -> None:
    restore_message = "Versioning-App: news\nVersioning-Kind: restore\nVersioning-Restored-From: " + "a" * 40
    commits = [
        _commit("a" * 40, "news: first build", 0),
        _commit("b" * 40, "news: add sports section", 60),
        _commit("c" * 40, "versioning: restore news", 120, restore_message),
    ]

    nodes = build_version_nodes(commits)

    restore_node = nodes[2]
    assert restore_node.parent_sha == "a" * 40
    assert restore_node.restored_from_sha == "a" * 40
    assert restore_node.kind == VersionKind.RESTORE
    assert restore_node.is_current
    assert nodes[1].is_set_aside
    assert not nodes[0].is_set_aside


def test_restore_trailer_pointing_outside_history_falls_back_to_previous_parent() -> None:
    restore_message = "Versioning-Restored-From: " + "f" * 40
    commits = [
        _commit("a" * 40, "news: first build", 0),
        _commit("b" * 40, "news: restore from elsewhere", 60, restore_message),
    ]

    nodes = build_version_nodes(commits)

    assert nodes[1].parent_sha == "a" * 40
    assert nodes[1].restored_from_sha is None


def test_lineage_walk_terminates_on_a_malformed_restore_trailer_cycle() -> None:
    self_restore_message = "Versioning-Restored-From: " + "b" * 40
    commits = [
        _commit("a" * 40, "news: first build", 0),
        _commit("b" * 40, "news: broken restore", 60, self_restore_message),
    ]

    nodes = build_version_nodes(commits)

    assert len(nodes) == 2


def test_title_comes_from_request_trailer_verbatim_when_present() -> None:
    message = "Versioning-App: news\nVersioning-Kind: change\nVersioning-Request: the digest now arrives at 7am"
    commits = [_commit("a" * 40, "news: adjust digest scheduling internals", 0, message)]

    nodes = build_version_nodes(commits)

    assert nodes[0].raw_title == "the digest now arrives at 7am"
    assert nodes[0].is_titled_by_request


def test_title_falls_back_to_subject_and_is_marked_untitled() -> None:
    commits = [_commit("a" * 40, "news: adjust digest scheduling internals", 0)]

    nodes = build_version_nodes(commits)

    assert nodes[0].raw_title == "news: adjust digest scheduling internals"
    assert not nodes[0].is_titled_by_request


def test_relative_time_label_covers_each_magnitude() -> None:
    now = _BASE_TIME
    assert relative_time_label(now - timedelta(seconds=30), now) == "just now"
    assert relative_time_label(now - timedelta(minutes=5), now) == "5 min ago"
    assert relative_time_label(now - timedelta(hours=1), now) == "1 hour ago"
    assert relative_time_label(now - timedelta(hours=7), now) == "7 hours ago"
    assert relative_time_label(now - timedelta(days=1), now) == "1 day ago"
    assert relative_time_label(now - timedelta(days=12), now) == "12 days ago"
    assert relative_time_label(now - timedelta(days=70), now) == "2 months ago"


def test_short_relative_time_label_covers_each_magnitude() -> None:
    now = _BASE_TIME
    assert short_relative_time_label(now - timedelta(seconds=30), now) == "now"
    assert short_relative_time_label(now - timedelta(minutes=5), now) == "5 min"
    assert short_relative_time_label(now - timedelta(hours=1), now) == "1 hr"
    assert short_relative_time_label(now - timedelta(hours=17), now) == "17 hrs"
    assert short_relative_time_label(now - timedelta(days=1), now) == "1 day"
    assert short_relative_time_label(now - timedelta(days=12), now) == "12 days"
    assert short_relative_time_label(now - timedelta(days=70), now) == "2 mo"


def test_discover_apps_includes_the_system_shell_titled_system(tmp_path: Path) -> None:
    for app_dir in ("news_reader", "system_interface"):
        (tmp_path / "system/apps" / app_dir).mkdir(parents=True)
    apps = discover_apps(tmp_path, tmp_path / "missing_apps.toml")

    title_by_name = {app.name: app.title for app in apps}
    assert title_by_name == {"news-reader": "News Reader", "system-interface": "System"}


def test_discover_apps_carries_the_registered_program_and_icon(tmp_path: Path) -> None:
    for app_dir in ("news_reader", "quiet_app"):
        (tmp_path / "system/apps" / app_dir).mkdir(parents=True)
    apps_toml = tmp_path / "apps.toml"
    apps_toml.write_text(
        '[[apps]]\nname = "news-reader"\nprogram = "news-reader"\nicon = "<svg viewBox=\\"0 0 24 24\\"/>"\n'
    )
    app_by_name = {app.name: app for app in discover_apps(tmp_path, apps_toml)}

    assert app_by_name["news-reader"].program == "news-reader"
    assert app_by_name["news-reader"].icon == '<svg viewBox="0 0 24 24"/>'
    assert app_by_name["quiet-app"].program is None
    assert app_by_name["quiet-app"].icon is None
