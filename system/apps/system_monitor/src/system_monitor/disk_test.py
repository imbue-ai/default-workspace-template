"""Tests for the on-demand walk of the disk: the rows and what they list, a row that cannot be measured, the
real ``du``, and the scanner that runs one walk at a time."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

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
    (home / ".mngr").mkdir()
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
        str(home / ".mngr"): 1000,
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
        ("home", 1000 * _MIB),
        ("other", 2900 * _MIB),
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
    assert by_id["home"]["items"] == [
        {"name": ".mngr", "hint": "chats' records and logs", "bytes": 1000 * _MIB},
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
            "hint": "not part of this workspace, so it cannot be freed from here",
            "bytes": 1400 * _MIB,
        },
    ]
    # The workspace is measured as its top-level entries in one du, without the app data and
    # uploads that get rows of their own; the system software without the home tree and the caches.
    assert measured[0] == (
        sorted(workspace.iterdir()),
        [workspace / "data" / ".apps", workspace / "data" / "uploads"],
    )
    # The home tree beside the workspace as its top-level entries, less the caches.
    assert measured[-2] == (
        [home / ".mngr"],
        [home / cache for cache in disk.CACHE_DIRS],
    )
    assert measured[-1] == (
        [root],
        [home, root / disk.OFF_VOLUME_CACHE_DIR, *disk.SYSTEM_EXCLUDES],
    )


@pytest.mark.parametrize(
    ("real_cache", "linked_cache", "listed_as"),
    [
        # docker, modal and imbue_cloud: ~/.cache links to the off-volume cache.
        ("var/cache/user", "home/user/.cache", "/var/cache/user"),
        # lima: the off-volume cache links to ~/.cache, a subvolume on the data disk.
        ("home/user/.cache", "var/cache/user", ".cache"),
    ],
)
# imbue_cloud: /home/user links into the volume, which is outside the root the system walk measures.
@pytest.mark.parametrize("home_is_a_link", [False, True])
def test_the_package_caches_and_the_home_folder_are_each_counted_once(
    tmp_path: Path,
    real_cache: str,
    linked_cache: str,
    listed_as: str,
    home_is_a_link: bool,
) -> None:
    root = tmp_path / "root"
    home = root / "home" / "user"
    if home_is_a_link:
        (tmp_path / "mngr-vol" / "home").mkdir(parents=True)
        home.parent.mkdir(parents=True)
        home.symlink_to(tmp_path / "mngr-vol" / "home")
    (home / "workspace").mkdir(parents=True)
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "tool").write_bytes(b"x" * _MIB)
    (home / ".mngr").mkdir()
    (home / ".mngr" / "log").write_bytes(b"x" * 2 * _MIB)
    # A loose file counts toward the home folder's row but is not listed among its folders.
    (home / ".claude.json").write_bytes(b"x" * _MIB)
    (root / real_cache / "uv").mkdir(parents=True)
    (root / real_cache / "uv" / "wheels").write_bytes(b"x" * 3 * _MIB)
    (root / linked_cache).parent.mkdir(parents=True, exist_ok=True)
    (root / linked_cache).symlink_to(root / real_cache)
    # A cache nested under a home entry, which the home tree's walk leaves out by --exclude.
    (home / ".local" / "share" / "uv").mkdir(parents=True)
    (home / ".local" / "share" / "uv" / "python").write_bytes(b"x" * 2 * _MIB)

    rows, missing = disk.scan(100 * _MIB, home / "workspace", home, disk.du_bytes, root)

    assert missing == []
    by_id = {row["id"]: row for row in rows}
    assert 5 * _MIB <= by_id["caches"]["bytes"] < 6 * _MIB
    cache_items = by_id["caches"]["items"]
    assert [(item["name"], item["hint"]) for item in cache_items] == [
        (listed_as, "downloads and build caches"),
        (".local/share/uv", "Python packages"),
    ]
    assert 3 * _MIB <= cache_items[0]["bytes"] < 4 * _MIB
    assert 2 * _MIB <= cache_items[1]["bytes"] < 3 * _MIB
    assert 3 * _MIB <= by_id["home"]["bytes"] < 4 * _MIB
    assert [item["name"] for item in by_id["home"]["items"]] == [".mngr"]
    software = by_id["other"]["items"][0]
    assert software["name"] == "System software"
    assert _MIB <= software["bytes"] < 2 * _MIB


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

    assert missing == ["workspace", "apps", "home"]
    assert [row["id"] for row in rows] == ["uploads", "apps", "caches", "other"]
    # The App data row keeps the apps it could measure.
    assert rows[1]["items"] == [{"name": "recipe-box", "hint": "", "bytes": 9000}]
    # The rest is nothing, and lists nothing when the system software cannot be measured.
    assert rows[-1]["bytes"] == 0 and rows[-1]["items"] == []


# The home tree's du fails, or its listing does (a home that is not a directory cannot be listed), or
# the workspace's du does.
@pytest.mark.parametrize(
    ("failing", "missing_row"),
    [("home_du", "home"), ("home_listing", "home"), ("workspace_du", "workspace")],
)
def test_a_row_that_cannot_be_measured_is_not_listed_as_space_that_cannot_be_freed(
    tmp_path: Path, failing: str, missing_row: str
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    if failing == "home_listing":
        home.write_text("")
    else:
        home.mkdir()
        (home / ".mngr").mkdir()
    failing_paths = {"home_du": [home / ".mngr"], "workspace_du": [workspace]}.get(
        failing
    )

    def measure(paths: list[Path], excludes: list[Path]) -> disk.Measured | None:
        if paths == failing_paths:
            return None
        return disk.Measured(10 * _MIB, {})

    rows, missing = disk.scan(100 * _MIB, workspace, home, measure, tmp_path / "root")

    assert missing == [missing_row]
    # The missing row's bytes are in the rest, so the rest is not called something that cannot be freed.
    assert rows[-1]["items"] == [
        {"name": "System software", "hint": "the workspace's tools", "bytes": 10 * _MIB}
    ]


@pytest.mark.parametrize(
    "system",
    [
        # The system walk fails.
        None,
        # lima: the system software is on the boot disk, not the data disk the workspace and home
        # folder are on, so the walk is bigger than what the rows above leave of the 100 MiB.
        disk.Measured(500 * _MIB, {}),
    ],
)
def test_a_system_walk_that_fails_or_is_bigger_than_the_rest_leaves_the_rest_unlisted(
    tmp_path: Path, system: disk.Measured | None
) -> None:
    home, root = tmp_path / "home", tmp_path / "root"
    (home / ".mngr").mkdir(parents=True)

    def measure(paths: list[Path], excludes: list[Path]) -> disk.Measured | None:
        return system if paths == [root] else disk.Measured(10 * _MIB, {})

    rows, missing = disk.scan(100 * _MIB, tmp_path / "workspace", home, measure, root)

    assert missing == []
    # The rows above take 40 MiB of the 100.
    assert rows[-1]["bytes"] == 60 * _MIB and rows[-1]["items"] == []


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
