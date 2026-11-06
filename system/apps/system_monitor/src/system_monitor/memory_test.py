"""Tests for the disk reader."""

from pathlib import Path

from system_monitor import memory


def test_disk_reports_the_size_used_and_free_of_the_path_s_filesystem(
    tmp_path: Path,
) -> None:
    figures = memory.disk(tmp_path)

    assert set(figures) == {"total", "used", "free"}
    assert figures["total"] > 0
    assert figures["used"] + figures["free"] <= figures["total"]
