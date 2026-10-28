"""Tests for the on-demand walk of the disk: the rows and what they list, a row that cannot be measured, the
real ``du``, and the scanner that runs one walk at a time."""

from datetime import datetime, timezone
from pathlib import Path

from system_monitor import disk

_MIB = 1024 * 1024


def _measure_from(sizes: dict[str, int]) -> disk.Measure:
    def measure(paths: list[Path], excludes: list[Path]) -> disk.Measured | None:
        by_path = {path: sizes.get(str(path), 0) * _MIB for path in paths}
        return disk.Measured(sum(by_path.values()), by_path)

    return measure


def test_the_rows_cover_the_buckets_and_the_rest_of_the_disk(tmp_path: Path) -> None:
    workspace, home, root = tmp_path / "workspace", tmp_path / "home", tmp_path / "root"
    for app in ("recipe-box", "habit-tracker"):
        (workspace / "data" / ".apps" / app).mkdir(parents=True)
    for folder in (".venv", "system", "docs"):
        (workspace / folder).mkdir()
    (workspace / "uv.lock").write_text("x")
    (home / ".cache").mkdir(parents=True)
    root.mkdir()
    sizes = {
        str(workspace / ".venv"): 2000,
        str(workspace / "system"): 700,
        str(workspace / "data"): 250,
        str(workspace / "uv.lock"): 50,
        str(workspace / "data" / "uploads"): 1200,
        str(workspace / "data" / ".apps" / "recipe-box"): 610,
        str(workspace / "data" / ".apps" / "habit-tracker"): 290,
        str(home / ".cache"): 2400,
        str(home / ".local/share/uv"): 600,
        str(root): 1500,
    }
    measured: list[tuple[list[Path], list[Path]]] = []

    def measure(paths: list[Path], excludes: list[Path]) -> disk.Measured | None:
        measured.append((paths, excludes))
        return _measure_from(sizes)(paths, excludes)

    rows, missing = disk.scan(12_000 * _MIB, workspace, home, measure, root)

    assert missing == []
    assert [(row["id"], row["bytes"]) for row in rows] == [
        ("workspace", 3000 * _MIB),
        ("uploads", 1200 * _MIB),
        ("apps", 900 * _MIB),
        ("caches", 3000 * _MIB),
        ("other", 3900 * _MIB),
    ]
    by_id = {row["id"]: row for row in rows}
    # The workspace's folders, largest first, with what the user would not know explained;
    # its loose files count toward the row but are not listed.
    assert by_id["workspace"]["items"] == [
        {
            "name": ".venv",
            "hint": "installed packages, rebuilt on demand",
            "bytes": 2000 * _MIB,
        },
        {"name": "system", "hint": "the workspace's own code", "bytes": 700 * _MIB},
        {"name": "data", "hint": "documents, notes, settings", "bytes": 250 * _MIB},
    ]
    assert by_id["apps"]["items"] == [
        {"name": "recipe-box", "hint": "", "bytes": 610 * _MIB},
        {"name": "habit-tracker", "hint": "", "bytes": 290 * _MIB},
    ]
    assert by_id["caches"]["items"] == [
        {"name": ".cache", "hint": "downloads and build caches", "bytes": 2400 * _MIB},
        {"name": ".local/share/uv", "hint": "Python packages", "bytes": 600 * _MIB},
    ]
    # The rest: the machine's system software, then whatever else the disk holds.
    assert by_id["other"]["items"] == [
        {
            "name": "System software",
            "hint": "the workspace's tools",
            "bytes": 1500 * _MIB,
        },
        {
            "name": "Other files on the machine's disk",
            "hint": "outside this workspace",
            "bytes": 2400 * _MIB,
        },
    ]
    # The workspace is measured as its top-level entries in one du, without the app data and
    # uploads that get rows of their own; the system software without the home tree.
    assert measured[0] == (
        sorted(workspace.iterdir()),
        [workspace / "data" / ".apps", workspace / "data" / "uploads"],
    )
    assert measured[-1] == ([root], [home, *disk.SYSTEM_EXCLUDES])


def test_a_row_that_cannot_be_measured_is_reported_missing_and_the_rest_never_goes_negative(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    for app in ("recipe-box", "habit-tracker"):
        (workspace / "data" / ".apps" / app).mkdir(parents=True)

    def measure(paths: list[Path], excludes: list[Path]) -> disk.Measured | None:
        if paths in (
            sorted(workspace.iterdir()),
            [workspace / "data" / ".apps" / "habit-tracker"],
            [tmp_path / "root"],
        ):
            return None
        return disk.Measured(9000, {path: 9000 for path in paths})

    rows, missing = disk.scan(
        100, workspace, tmp_path / "home", measure, tmp_path / "root"
    )

    assert missing == ["workspace", "apps"]
    assert [row["id"] for row in rows] == ["uploads", "apps", "caches", "other"]
    # The App data row keeps the apps it could measure.
    assert rows[1]["items"] == [{"name": "recipe-box", "hint": "", "bytes": 9000}]
    # The rest is nothing, and lists nothing when the system software cannot be measured.
    assert rows[-1]["bytes"] == 0 and rows[-1]["items"] == []


def test_du_measures_a_tree_less_its_excludes_and_a_missing_path_is_nothing(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "tree"
    (tree / "keep").mkdir(parents=True)
    (tree / "skip").mkdir()
    (tree / "keep" / "big").write_bytes(b"x" * 200_000)
    (tree / "skip" / "bigger").write_bytes(b"x" * 400_000)

    whole = disk.du_bytes([tree], [])
    kept = disk.du_bytes([tree], [tree / "skip"])
    parts = disk.du_bytes([tree / "keep", tree / "skip"], [])

    assert whole is not None and kept is not None and parts is not None
    assert whole.total >= 600_000 and 200_000 <= kept.total < 400_000
    assert whole.by_path == {tree: whole.total}
    assert parts.by_path[tree / "skip"] > parts.by_path[tree / "keep"] >= 200_000
    assert parts.total == sum(parts.by_path.values())
    assert disk.du_bytes([tmp_path / "missing"], []) == disk.Measured(0, {})


def test_the_scanner_runs_one_walk_at_a_time_and_keeps_the_result_with_its_time() -> (
    None
):
    started = []
    scanner = disk.DiskScanner(
        run=lambda: (
            started.append(1),
            (
                [{"id": "other", "label": "x", "hint": "", "bytes": 1, "items": []}],
                ["caches"],
            ),
        )[1],
        clock=lambda: datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc),
    )

    assert scanner.snapshot() == {
        "state": "idle",
        "scanned_at": None,
        "rows": [],
        "missing": [],
    }
    assert scanner.start() is True
    scanner.wait(5.0)
    result = scanner.snapshot()

    assert result["state"] == "done"
    assert result["scanned_at"] == "2026-10-20T15:00:00+00:00"
    assert result["rows"][0]["id"] == "other" and result["missing"] == ["caches"]
    assert started == [1]


def test_a_walk_that_cannot_read_the_disk_ends_failed_rather_than_scanning() -> None:
    def run() -> tuple[list[dict[str, object]], list[str]]:
        raise OSError("no such disk")

    scanner = disk.DiskScanner(
        run=run, clock=lambda: datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    )

    assert scanner.start() is True
    scanner.wait(5.0)

    assert scanner.snapshot() == {
        "state": "failed",
        "scanned_at": "2026-10-20T15:00:00+00:00",
        "rows": [],
        "missing": [],
    }
    # A failed walk does not block the next one.
    assert scanner.start() is True
    scanner.wait(5.0)
