"""Deleting: only what the latest measurement listed, and only what its verdict allows.

Every test here plants its own folders under a temporary directory and points the measurement at them, so nothing
the app could delete is anything but the test's own.
"""

import subprocess
from pathlib import Path

import pytest

from disk_space import cleanup, scan, store
from disk_space.testing import (
    GIB,
    a_disk,
    a_node,
    a_saved_item,
    install_disk,
    latest_measurement,
    save_measurement,
)


def _measurement(*nodes: dict) -> dict:
    root = a_node("root", None, "Whole disk")
    return save_measurement([root, *nodes])


def _safe_item(item_id: str, path: str, size: int = 10 * GIB) -> dict:
    return a_node(
        item_id,
        "root",
        "Downloaded packages",
        size=size,
        path=path,
        verdict=scan.SAFE,
        item=True,
        method="remove",
    )


def test_a_safe_item_is_deleted_and_the_deletion_is_on_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path)
    _measurement(_safe_item("n1", path))
    install_disk(
        monkeypatch, a_disk(free=7 * GIB, percent=93), a_disk(free=8 * GIB, percent=92)
    )
    result = cleanup.delete(
        [path], user_approved=False, by="page", requested_by="user:someone"
    )
    assert result["deleted"] == [
        {
            "path": path,
            "name": "Downloaded packages",
            "bytes": 10 * GIB,
            "verdict": "safe",
        }
    ]
    assert result["refused"] == []
    assert not folder.exists()
    assert result["freed_bytes"] == 1 * GIB
    entry = store.read_history()[0]
    assert entry["kind"] == "delete"
    assert entry["by"] == "page"
    assert entry["requested_by"] == "user:someone"
    assert entry["deleted"] == result["deleted"]
    assert entry["freed_bytes"] == 1 * GIB
    assert entry["summary"] == "You deleted 1 item (Downloaded packages)."


def test_the_measurement_stops_listing_what_was_deleted_and_grows_its_parents_smaller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, _ = a_saved_item(tmp_path)
    snapshot = save_measurement(
        [
            a_node("root", None, "Whole disk", size=100 * GIB),
            a_node(
                "n1",
                "root",
                "Downloaded packages",
                size=10 * GIB,
                path=path,
                verdict=scan.SAFE,
                item=True,
                method="remove",
            ),
            a_node("n2", "root", "Everything else", size=90 * GIB),
        ]
    )
    snapshot["nodes"][0]["safe_bytes"] = 10 * GIB
    store.rewrite_latest_snapshot(snapshot)
    install_disk(monkeypatch, a_disk(free=7 * GIB), a_disk(free=8 * GIB))
    cleanup.delete([path], user_approved=False, by="page")
    after = latest_measurement()
    assert [node["id"] for node in after["nodes"]] == ["root", "n2"]
    assert after["nodes"][0]["bytes"] == 90 * GIB
    assert after["nodes"][0]["safe_bytes"] == 0
    assert after["edited_at"] > 0
    assert after["disk"]["free"] == 8 * GIB


def test_a_folder_needing_the_users_yes_is_left_alone_without_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path, name="old-project")
    _measurement(
        a_node(
            "n1",
            "root",
            "Project copy",
            size=GIB,
            path=path,
            verdict=scan.ASK,
            item=True,
        )
    )
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([path], user_approved=False, by="page")
    assert result["deleted"] == []
    assert result["refused"] == [
        {
            "path": path,
            "reason": "It is in Ask first: only you can approve deleting it.",
        }
    ]
    assert folder.exists()
    assert store.read_history() == []


def test_a_folder_needing_the_users_yes_goes_once_the_user_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path, name="old-project")
    _measurement(
        a_node(
            "n1",
            "root",
            "Project copy",
            size=GIB,
            path=path,
            verdict=scan.ASK,
            item=True,
            method="remove",
        )
    )
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([path], user_approved=True, by="helper")
    assert [item["path"] for item in result["deleted"]] == [path]
    assert not folder.exists()
    assert store.read_history()[0]["summary"].startswith("The cleanup helper deleted")


def test_a_kept_folder_is_never_deleted_even_with_the_users_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path, name="the-workspace-itself")
    _measurement(
        a_node(
            "n1",
            "root",
            "Workspace code",
            size=GIB,
            path=path,
            verdict=scan.KEEP,
            item=True,
        )
    )
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([path], user_approved=True, by="helper")
    assert result["refused"] == [
        {"path": path, "reason": "It is in Keep: deleting it breaks things."}
    ]
    assert folder.exists()


def test_a_chat_is_never_deleted_as_a_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path, name="agent-1")
    _measurement(
        a_node(
            "n1",
            "root",
            "Chat: Gardening",
            size=GIB,
            path=path,
            verdict=scan.ASK,
            item=True,
        )
    )
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([path], user_approved=True, by="helper")
    assert result["refused"] == [
        {
            "path": path,
            "reason": "Chats are ended through the cleanup helper, not deleted here.",
        }
    ]
    assert folder.exists()


def test_a_folder_the_measurement_did_not_list_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, folder = a_saved_item(tmp_path, name="real")
    _measurement(
        a_node(
            "n1",
            "root",
            "Downloaded packages",
            size=GIB,
            verdict=scan.SAFE,
            item=True,
            method="remove",
        )
    )
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([str(folder)], user_approved=True, by="page")
    assert result["deleted"] == []
    assert result["refused"] == [
        {
            "path": str(folder),
            "reason": "It is not one of the items the last measurement listed.",
        }
    ]
    assert folder.exists()


def test_a_folder_inside_a_listed_item_is_not_itself_an_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path, name="cache")
    _measurement(_safe_item("n1", path))
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([str(folder / "blob.txt")], user_approved=True, by="page")
    assert result["deleted"] == []
    assert folder.exists()


def test_nothing_is_deleted_before_the_first_measurement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path)
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([path], user_approved=True, by="page")
    assert result == {
        "deleted": [],
        "refused": [{"path": path, "reason": "Nothing has been measured yet."}],
        "freed_bytes": 0,
    }
    assert folder.exists()


def test_a_link_pointing_somewhere_else_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "somewhere-else"
    target.mkdir()
    (target / "important.txt").write_text("keep me")
    link = tmp_path / "linked-cache"
    link.symlink_to(target)
    _measurement(_safe_item("n1", str(link)))
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([str(link)], user_approved=False, by="page")
    assert result["refused"] == [
        {"path": str(link), "reason": "It points somewhere else, so it is left alone."}
    ]
    assert (target / "important.txt").read_text() == "keep me"


def test_a_file_listed_as_an_item_is_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = tmp_path / "old.tar"
    archive.write_bytes(b"x" * 100)
    _measurement(_safe_item("n1", str(archive)))
    install_disk(monkeypatch, a_disk())
    assert cleanup.delete([str(archive)], user_approved=False, by="page")["deleted"]
    assert not archive.exists()


def test_the_package_downloads_are_cleared_by_uv_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    (cache / "archive").mkdir()
    _measurement(
        a_node(
            "n1",
            "root",
            "Python package downloads",
            size=GIB,
            path=str(cache),
            verdict=scan.SAFE,
            item=True,
            method="uv-cache",
        )
    )
    install_disk(monkeypatch, a_disk())
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)
    result = cleanup.delete([str(cache)], user_approved=False, by="page")
    assert calls == [["uv", "cache", "clean", "--cache-dir", str(cache)]]
    assert result["deleted"][0]["path"] == str(cache)


def test_a_package_cache_uv_could_not_clear_is_reported_rather_than_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    _measurement(
        a_node(
            "n1",
            "root",
            "Python package downloads",
            size=GIB,
            path=str(cache),
            verdict=scan.SAFE,
            item=True,
            method="uv-cache",
        )
    )
    install_disk(monkeypatch, a_disk())

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, "", "error: cache is busy\n")

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)
    result = cleanup.delete([str(cache)], user_approved=False, by="page")
    assert result["deleted"] == []
    assert result["refused"] == [{"path": str(cache), "reason": "error: cache is busy"}]
    assert store.read_history() == []


def test_a_package_cache_uv_could_not_run_in_time_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    _measurement(
        a_node(
            "n1",
            "root",
            "Python package downloads",
            size=GIB,
            path=str(cache),
            verdict=scan.SAFE,
            item=True,
            method="uv-cache",
        )
    )
    install_disk(monkeypatch, a_disk())

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, cleanup.UV_CLEAN_TIMEOUT_SECONDS)

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)
    monkeypatch.setattr(cleanup, "_cache_in_use", lambda _cache: False)
    result = cleanup.delete([str(cache)], user_approved=False, by="page")
    assert result["refused"] == [
        {
            "path": str(cache),
            "reason": "The Python package cache was busy; try again in a few minutes.",
        }
    ]


def test_the_backup_cache_is_left_alone_while_a_backup_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = tmp_path / "restic"
    cache.mkdir()
    _measurement(_safe_item("n1", str(cache)))
    install_disk(monkeypatch, a_disk())
    monkeypatch.setattr(cleanup, "_is_backup_running", lambda: True)
    result = cleanup.delete([str(cache)], user_approved=False, by="page")
    assert result["refused"] == [
        {
            "path": str(cache),
            "reason": "A backup is running right now; try again once it finishes.",
        }
    ]
    assert cache.exists()
    monkeypatch.setattr(cleanup, "_is_backup_running", lambda: False)
    assert cleanup.delete([str(cache)], user_approved=False, by="page")["deleted"]
    assert not cache.exists()


def test_a_folder_that_vanished_before_its_turn_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path)
    _measurement(_safe_item("n1", path))
    install_disk(monkeypatch, a_disk())
    for child in folder.iterdir():
        child.unlink()
    folder.rmdir()
    result = cleanup.delete([path], user_approved=False, by="page")
    assert [item["path"] for item in result["deleted"]] == [path]
    assert result["refused"] == []


def test_one_refusal_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    good, good_folder = a_saved_item(tmp_path, name="cache")
    kept, kept_folder = a_saved_item(tmp_path, name="source-code")
    _measurement(
        _safe_item("n1", good),
        a_node(
            "n2",
            "root",
            "Workspace code",
            size=GIB,
            path=kept,
            verdict=scan.KEEP,
            item=True,
        ),
    )
    install_disk(monkeypatch, a_disk())
    result = cleanup.delete([kept, good], user_approved=False, by="page")
    assert [item["path"] for item in result["deleted"]] == [good]
    assert [item["path"] for item in result["refused"]] == [kept]
    assert not good_folder.exists()
    assert kept_folder.exists()


def test_the_freed_space_is_read_again_while_the_disk_figures_catch_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path, folder = a_saved_item(tmp_path)
    _measurement(_safe_item("n1", path))
    monkeypatch.setattr(cleanup, "FREED_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cleanup, "FREED_SETTLE_SECONDS", 5.0)
    # Before; right after (not moved yet); then caught up.
    install_disk(
        monkeypatch,
        a_disk(free=7 * GIB, percent=93),
        a_disk(free=7 * GIB, percent=93),
        a_disk(free=7 * GIB, percent=93),
        a_disk(free=8 * GIB, percent=92),
    )
    result = cleanup.delete([path], user_approved=False, by="page")
    assert not folder.exists()
    assert result["freed_bytes"] == 1 * GIB


def _uv_cache_measurement(tmp_path: Path) -> Path:
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    _measurement(
        a_node(
            "n1",
            "root",
            "Python package downloads",
            size=GIB,
            path=str(cache),
            verdict=scan.SAFE,
            item=True,
            method="uv-cache",
        )
    )
    return cache


def test_a_package_cache_marked_in_use_but_used_by_nothing_is_cleared_by_force(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = _uv_cache_measurement(tmp_path)
    install_disk(monkeypatch, a_disk())
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--force" not in argv:
            raise subprocess.TimeoutExpired(argv, cleanup.UV_POLITE_WAIT_SECONDS)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)
    monkeypatch.setattr(cleanup, "_cache_in_use", lambda _cache: False)
    result = cleanup.delete([str(cache)], user_approved=False, by="page")
    assert calls == [
        ["uv", "cache", "clean", "--cache-dir", str(cache)],
        ["uv", "cache", "clean", "--cache-dir", str(cache), "--force"],
    ]
    assert result["deleted"][0]["path"] == str(cache)


def test_a_package_cache_a_running_program_uses_is_never_forced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = _uv_cache_measurement(tmp_path)
    install_disk(monkeypatch, a_disk())
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, cleanup.UV_POLITE_WAIT_SECONDS)

    monkeypatch.setattr(cleanup.subprocess, "run", fake_run)
    monkeypatch.setattr(cleanup, "_cache_in_use", lambda _cache: True)
    result = cleanup.delete([str(cache)], user_approved=False, by="page")
    assert all("--force" not in argv for argv in calls)
    assert result["deleted"] == []
    assert result["refused"][0]["reason"].startswith(
        "A running program is using the Python package cache"
    )


def test_a_program_running_from_inside_a_folder_counts_as_using_it(
    tmp_path: Path,
) -> None:
    holder = tmp_path / "cache" / "env"
    holder.mkdir(parents=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    process = subprocess.Popen(["sleep", "30"], cwd=holder)
    try:
        assert cleanup._cache_in_use(tmp_path / "cache") is True
        assert cleanup._cache_in_use(other) is False
    finally:
        process.kill()
        process.wait()


def test_holding_only_the_caches_lock_file_is_not_using_it(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / ".lock").write_text("")
    with (cache / ".lock").open() as lock:
        process = subprocess.Popen(["sleep", "30"], stdin=lock, cwd=tmp_path)
    try:
        assert cleanup._cache_in_use(cache) is False
    finally:
        process.kill()
        process.wait()
