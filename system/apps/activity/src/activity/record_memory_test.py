from datetime import datetime
from datetime import timezone
from pathlib import Path

from activity.history import MemorySample
from activity.history import read_history
from activity.memory_reading import MemorySource
from activity.memory_reading import MemorySources
from activity.record_memory import record_once

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _sources(tmp_path: Path) -> MemorySources:
    return MemorySources(
        host_meminfo_path=tmp_path / "host-meminfo",
        cgroup_dir=tmp_path / "cgroup",
        proc_meminfo_path=tmp_path / "meminfo",
    )


def test_one_reading_is_appended_as_the_page_would_read_it(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    sources.host_meminfo_path.write_text("MemTotal: 6815744 kB\nMemAvailable: 2097152 kB\n")
    history_path = tmp_path / "history.tsv"
    assert record_once(sources, history_path, _NOW) is True
    assert read_history(history_path) == [
        MemorySample(
            at_epoch_seconds=int(_NOW.timestamp()),
            used_kib=4_718_592,
            limit_kib=6_815_744,
            source=MemorySource.HOST_MEMINFO,
        )
    ]


def test_no_memory_source_records_nothing(tmp_path: Path) -> None:
    history_path = tmp_path / "history.tsv"
    assert record_once(_sources(tmp_path), history_path, _NOW) is False
    assert not history_path.exists()
