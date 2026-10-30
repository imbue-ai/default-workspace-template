import pytest
from inline_snapshot import snapshot

from disk_headroom.cli import format_disk_space
from disk_headroom.space import HostQuota, compute_disk_space

_GIB = 1024**3


@pytest.mark.parametrize(
    ("quota", "expected"),
    [
        pytest.param(
            None,
            snapshot(
                "free: 40.0 GiB of 100.0 GiB (60.0 GiB used)\nlimited by: the disk (no disk quota is published for it)"
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
