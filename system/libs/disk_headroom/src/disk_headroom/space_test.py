import shutil
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from disk_headroom.cli import format_disk_space
from disk_headroom.space import (
    HOST_DISKINFO_MAX_AGE_SECONDS,
    HostQuota,
    compute_disk_space,
    read_disk_space,
)

_NOW = 1_793_250_000.0
_GIB = 1024**3


def _write_diskinfo(path: Path, limit: int, used: int, published_at: float) -> None:
    # The fixed-width layout the cloud machine's publisher writes.
    path.write_text(
        f"QuotaLimitBytes: {limit:20d}\nQuotaUsedBytes: {used:20d}\nTimestamp: {int(published_at):15d}\n"
    )


def test_a_quota_with_less_room_than_the_disk_bounds_what_can_be_written(
    tmp_path: Path,
) -> None:
    diskinfo = tmp_path / ".host-diskinfo"
    _write_diskinfo(
        diskinfo,
        limit=41 * _GIB,
        used=41 * _GIB - 5 * 1024 * 1024,
        published_at=_NOW - 2,
    )

    space = read_disk_space(tmp_path, host_diskinfo=diskinfo, clock=lambda: _NOW)

    assert space.free == 5 * 1024 * 1024
    assert space.is_quota_bound
    assert space.used == 41 * _GIB - 5 * 1024 * 1024
    assert space.total == 41 * _GIB


def test_a_disk_with_less_room_than_the_quota_bounds_what_can_be_written(
    tmp_path: Path,
) -> None:
    # The quota leaves more room than any test machine's disk has free.
    diskinfo = tmp_path / ".host-diskinfo"
    _write_diskinfo(diskinfo, limit=10**18, used=25 * _GIB, published_at=_NOW)

    space = read_disk_space(tmp_path, host_diskinfo=diskinfo, clock=lambda: _NOW)

    assert space.free == space.filesystem_free
    assert not space.is_quota_bound
    assert space.quota_free == 10**18 - 25 * _GIB
    assert space.total == 25 * _GIB + space.free


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="no file"),
        pytest.param("stale", id="stale"),
        pytest.param("QuotaLimitBytes: 1\nTimestamp: 1793250000\n", id="incomplete"),
        pytest.param(
            "QuotaLimitBytes: x\nQuotaUsedBytes: y\nTimestamp: z\n", id="garbled"
        ),
        pytest.param(
            "QuotaLimitBytes: \u00b2\nQuotaUsedBytes: 1\nTimestamp: 1793250000\n",
            id="digit int() cannot parse",
        ),
    ],
)
def test_without_a_fresh_complete_file_the_filesystem_figures_are_the_answer(
    tmp_path: Path, content: str | None
) -> None:
    diskinfo = tmp_path / ".host-diskinfo"
    if content == "stale":
        _write_diskinfo(
            diskinfo,
            limit=_GIB,
            used=_GIB,
            published_at=_NOW - HOST_DISKINFO_MAX_AGE_SECONDS - 1,
        )
    elif content is not None:
        diskinfo.write_text(content)

    space = read_disk_space(tmp_path, host_diskinfo=diskinfo, clock=lambda: _NOW)

    # Free space moves between two statfs calls on a busy disk; the size does not.
    assert space.total == shutil.disk_usage(tmp_path).total
    assert space.free == space.filesystem_free
    assert space.quota_free is None


def test_a_path_on_another_filesystem_is_not_held_to_the_quota(tmp_path: Path) -> None:
    # /tmp in a workspace is a RAM disk the machine's quota does not cover; /dev
    # stands in for it here as a filesystem every test machine has beside its disk.
    diskinfo = tmp_path / ".host-diskinfo"
    _write_diskinfo(diskinfo, limit=_GIB, used=_GIB, published_at=_NOW)

    space = read_disk_space(Path("/dev"), host_diskinfo=diskinfo, clock=lambda: _NOW)

    assert space.quota_free is None
    assert space.free == space.filesystem_free


def test_a_quota_already_exceeded_leaves_nothing_to_write() -> None:
    space = compute_disk_space(
        100 * _GIB, 60 * _GIB, 40 * _GIB, HostQuota(limit=30 * _GIB, used=31 * _GIB)
    )

    assert space.free == 0
    assert space.is_quota_bound


@pytest.mark.parametrize(
    ("quota", "expected"),
    [
        pytest.param(
            None,
            snapshot(
                "free: 40.0 GiB of 100.0 GiB (60.0 GiB used)\nlimited by: the disk (no disk quota applies here)"
            ),
            id="no quota",
        ),
        pytest.param(
            HostQuota(limit=40 * _GIB, used=38 * _GIB),
            snapshot(
                "free: 2.0 GiB of 40.0 GiB (38.0 GiB used)\nlimited by: the machine's disk quota (2.0 GiB left); the disk itself reports 40.0 GiB free"
            ),
            id="quota bound",
        ),
        pytest.param(
            HostQuota(limit=200 * _GIB, used=60 * _GIB),
            snapshot(
                "free: 40.0 GiB of 100.0 GiB (60.0 GiB used)\nlimited by: the disk (40.0 GiB free); the machine's disk quota has 140.0 GiB left"
            ),
            id="disk bound",
        ),
    ],
)
def test_the_command_names_the_limit_that_runs_out_first(
    quota: HostQuota | None, expected: str
) -> None:
    assert (
        format_disk_space(compute_disk_space(100 * _GIB, 60 * _GIB, 40 * _GIB, quota))
        == expected
    )
