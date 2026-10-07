import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path

from activity.storage import StorageCategory
from activity.storage import WORKSPACE_CATEGORIES
from activity.storage import measure_storage
from activity.storage import parse_du_output
from activity.storage import summarize_storage
from activity.storage import user_file_paths
from activity.testing import FakeRunner
from activity.testing import completed

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def test_du_lines_are_read_as_sizes_by_path() -> None:
    assert parse_du_output("1640\t/var/cache/user\n12\tdata/uploads\nnot a line\n") == {
        "/var/cache/user": 1640,
        "data/uploads": 12,
    }


def test_categories_add_up_and_list_their_existing_folders_largest_first() -> None:
    categories = (
        StorageCategory(
            category_id="caches", name="Download caches", description="", paths=(Path("/c1"), Path("/c2"))
        ),
        StorageCategory(category_id="logs", name="Logs", description="", paths=(Path("/missing"),)),
    )
    summary = summarize_storage(
        categories=categories,
        size_kib_by_path={"/c1": 100, "/c2": 900},
        measured_at=_NOW,
        measure_seconds=0.42,
        command="du",
        notes=(),
    )
    assert summary.total_kib == 1000
    assert [(category.name, category.size_kib) for category in summary.categories] == [
        ("Download caches", 1000),
        ("Logs", 0),
    ]
    assert [folder.path for folder in summary.categories[0].folders] == ["/c2", "/c1"]
    assert [folder.path for folder in summary.largest] == ["/c2", "/c1"]
    assert summary.measure_seconds == 0.4


def test_the_users_files_are_the_visible_entries_of_data(tmp_path: Path) -> None:
    (tmp_path / "documents").mkdir()
    (tmp_path / ".state").mkdir()
    (tmp_path / "README.md").write_text("x")
    assert [path.name for path in user_file_paths(tmp_path)] == ["README.md", "documents"]
    assert user_file_paths(tmp_path / "missing") == ()


def test_measuring_runs_one_du_over_existing_folders_and_a_timeout_becomes_a_note(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    uploads = str((data_dir / "uploads").absolute())
    runner = FakeRunner({"du": completed(f"64\t{uploads}\n")})
    clock_values = iter([10.0, 11.5])
    summary = measure_storage(
        data_dir=data_dir, run_command=runner, now=lambda: _NOW, clock=lambda: next(clock_values)
    )
    assert runner.calls[0][:3] == ("du", "-sk", "--")
    assert uploads in runner.calls[0]
    files = next(category for category in summary.categories if category.category_id == "files")
    assert files.size_kib == 64
    assert summary.measure_seconds == 1.5

    assert summary.command.startswith("du -sk -- ") and uploads in summary.command


def test_a_timeout_keeps_the_folders_du_finished_and_says_the_total_is_incomplete(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "uploads").mkdir(parents=True)
    uploads = str((data_dir / "uploads").absolute())
    slow = FakeRunner({"du": subprocess.TimeoutExpired("du", 60, output=f"64\t{uploads}\n".encode())})
    timed_out = measure_storage(data_dir=data_dir, run_command=slow, now=lambda: _NOW, clock=lambda: 0.0)
    files = next(category for category in timed_out.categories if category.category_id == "files")
    assert files.size_kib == 64
    assert "total is incomplete" in timed_out.notes[0]


def test_the_categories_never_overlap_so_nothing_is_counted_twice() -> None:
    paths = [path for category in WORKSPACE_CATEGORIES for path in category.paths]
    for path in paths:
        for other in paths:
            assert path == other or not other.is_relative_to(path), (path, other)
    assert Path("/home/user/worktrees") in paths
