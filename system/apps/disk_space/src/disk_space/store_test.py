"""What the app keeps on disk: settings, the watch, the bounded history and the measurements it lives on."""

import json
from pathlib import Path

from disk_space import store
from disk_space.testing import a_disk, a_node, latest_measurement, save_measurement


def test_settings_start_at_ninety_percent_and_six_hours() -> None:
    assert store.read_settings() == {"threshold": 90, "renotify_hours": 6.0}


def test_the_alert_line_stays_between_fifty_and_ninety_nine_percent(
    data_dir: Path,
) -> None:
    assert store.write_settings(40)["threshold"] == 50
    assert store.write_settings(120)["threshold"] == 99
    assert store.write_settings(93)["threshold"] == 93
    assert store.read_settings()["threshold"] == 93


def test_a_settings_file_that_does_not_parse_falls_back_to_the_defaults(
    data_dir: Path,
) -> None:
    (data_dir / "settings.json").write_text("not json at all")
    assert store.read_settings()["threshold"] == 90
    (data_dir / "settings.json").write_text(json.dumps({"threshold": "very full"}))
    assert store.read_settings()["threshold"] == 90


def test_the_watch_is_armed_until_a_check_says_otherwise() -> None:
    assert store.read_watch()["armed"] is True
    store.update_watch(armed=False, last_alert_at=1234.0)
    watch = store.read_watch()
    assert watch["armed"] is False
    assert watch["last_alert_at"] == 1234.0
    store.update_watch(last_percent=91)
    assert store.read_watch()["last_alert_at"] == 1234.0


def test_history_records_everything_it_was_given_and_reads_back_newest_first() -> None:
    first = store.append_history(
        "delete", "You deleted 2 items.", freed_bytes=10, deleted=[{"path": "/x"}]
    )
    second = store.append_history("alert", "The disk passed 90%.")
    assert store.read_history() == [second, first]
    assert store.find_history(first["id"]) == first
    assert store.find_history("no such entry") is None
    assert len(first["id"]) == 12
    found = store.find_history(first["id"])
    assert found is not None
    assert found["freed_bytes"] == 10


def test_the_history_drops_its_oldest_entries_once_it_is_full(data_dir: Path) -> None:
    for index in range(store.HISTORY_KEPT + 25):
        store.append_history("delete", f"entry {index}")
    lines = (data_dir / "history.jsonl").read_text().splitlines()
    assert len(lines) == store.HISTORY_KEPT
    assert "entry 0" not in (data_dir / "history.jsonl").read_text()
    assert json.loads(lines[-1])["summary"] == f"entry {store.HISTORY_KEPT + 24}"
    assert store.read_history()[0]["summary"] == f"entry {store.HISTORY_KEPT + 24}"
    assert len(store.read_history()) == store.HISTORY_KEPT


def test_a_history_line_that_does_not_parse_is_skipped_rather_than_failing_the_read(
    data_dir: Path,
) -> None:
    store.append_history("delete", "kept")
    with (data_dir / "history.jsonl").open("a") as handle:
        handle.write("{not json\n")
    assert [entry["summary"] for entry in store.read_history()] == ["kept"]


def test_only_the_newest_measurements_are_kept(data_dir: Path) -> None:
    ids = [
        save_measurement([a_node("n0", None, "Whole disk")], taken_at=float(index))[
            "id"
        ]
        for index in range(8)
    ]
    kept = sorted(
        entry.name for entry in (data_dir / "snapshots").iterdir() if entry.is_dir()
    )
    assert kept == sorted(ids[-store.SNAPSHOTS_KEPT :])
    assert latest_measurement()["id"] == ids[-1]


def test_the_measurements_are_marked_rebuildable_so_the_backup_skips_them(
    data_dir: Path,
) -> None:
    assert not (data_dir / "snapshots").exists()
    save_measurement([a_node("n0", None, "Whole disk")])
    assert (data_dir / "snapshots" / ".nobackup").is_file()
    # A restore brings the directory back empty; measuring again fills it, and the marker with it.
    for entry in (data_dir / "snapshots").iterdir():
        if entry.is_dir():
            for child in entry.iterdir():
                child.unlink()
            entry.rmdir()
    (data_dir / "snapshots" / ".nobackup").unlink()
    save_measurement([a_node("n0", None, "Whole disk")])
    assert (data_dir / "snapshots" / ".nobackup").is_file()
    assert store.latest_snapshot() is not None


def test_a_measurement_read_by_a_name_that_climbs_out_of_its_folder_is_refused() -> (
    None
):
    assert store.read_snapshot("../../etc") is None
    assert store.read_snapshot(".hidden") is None
    save_measurement([a_node("n0", None, "Whole disk")])
    assert store.read_snapshot(latest_measurement()["id"]) is not None
    assert store.read_snapshot("20240101T000000-nope") is None


def test_rewriting_the_latest_measurement_keeps_its_id_and_edited_nodes() -> None:
    snapshot = save_measurement([a_node("n0", None, "Whole disk", size=10)])
    snapshot["nodes"][0]["bytes"] = 12
    store.rewrite_latest_snapshot(snapshot)
    assert latest_measurement()["nodes"][0]["bytes"] == 12


def test_a_lock_is_held_by_one_holder_at_a_time() -> None:
    assert store.is_locked("scan") is False
    with store.held_lock("scan", wait=False) as held:
        assert held is True
        assert store.is_locked("scan") is True
        with store.held_lock("scan", wait=False) as again:
            assert again is False
    assert store.is_locked("scan") is False


def test_the_data_directory_is_created_on_first_use(data_dir: Path) -> None:
    (data_dir / "settings.json").unlink(missing_ok=True)
    data_dir.rmdir()
    assert not data_dir.exists()
    assert store.data_dir().is_dir()


def test_a_measurement_saved_without_a_disk_still_answers_a_disk() -> None:
    save_measurement(
        [a_node("n0", None, "Whole disk")],
        disk=a_disk(total=10, used=9, free=1, percent=90),
    )
    assert latest_measurement()["disk"] == a_disk(total=10, used=9, free=1, percent=90)
