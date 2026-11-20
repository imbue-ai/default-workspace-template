from pathlib import Path

import pytest

from app_store.room import RoomVerdict
from app_store.room import SystemResourceReader
from app_store.room import WorkspaceRoom
from app_store.room import check_room
from app_store.room import format_mb
from app_store.room import measure_directory_mb

_ROOM = WorkspaceRoom(disk_free_mb=5_000, memory_available_mb=2_000)


def test_an_app_with_room_to_spare_fits() -> None:
    check = check_room("Demo", disk_mb=100, memory_mb=100, room=_ROOM)
    assert (check.verdict, check.blocked_reason) == (RoomVerdict.FITS, "")


@pytest.mark.parametrize(("disk_mb", "memory_mb"), [(4_500, 100), (100, 1_600)])
def test_an_app_leaving_little_disk_or_memory_is_tight(disk_mb: int, memory_mb: int) -> None:
    check = check_room("Demo", disk_mb=disk_mb, memory_mb=memory_mb, room=_ROOM)
    assert (check.verdict, check.blocked_reason) == (RoomVerdict.TIGHT, "")


def test_an_app_needing_more_than_is_free_wont_fit_and_says_why() -> None:
    memory = check_room("Demo", disk_mb=100, memory_mb=2_500, room=_ROOM)
    assert memory.verdict is RoomVerdict.WONT_FIT
    assert memory.blocked_reason == (
        "Demo needs about 2.4 GB of memory while in use, and 2.0 GB is available. "
        "Uninstall an app or close a few windows to make room."
    )
    both = check_room("Demo", disk_mb=6_000, memory_mb=2_500, room=_ROOM)
    assert (
        "memory while in use, and 2.0 GB is available; and about 5.9 GB of disk, and 4.9 GB is free"
        in both.blocked_reason
    )


def test_sizes_read_as_mb_below_a_gigabyte_and_gb_above() -> None:
    assert [format_mb(mb) for mb in (0, 950, 1024, 1536)] == ["0 MB", "950 MB", "1.0 GB", "1.5 GB"]


def test_a_folders_size_is_measured_from_its_files(tmp_path: Path) -> None:
    assert measure_directory_mb(tmp_path / "missing") == 0
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "big.bin").write_bytes(b"\1" * (3 * 1024 * 1024 + 10))
    (tmp_path / "small.txt").write_text("x")
    assert 3 <= measure_directory_mb(tmp_path) <= 5


def test_the_system_reader_reads_real_disk_and_memory(tmp_path: Path) -> None:
    room = SystemResourceReader(disk_path=tmp_path / "not" / "made" / "yet").read_room()
    assert room.disk_free_mb > 0
    assert room.memory_available_mb > 0
