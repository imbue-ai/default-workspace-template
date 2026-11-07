import gzip
import re
from pathlib import Path

from event_streams.archive import compress_staged_streams, stage_oversized_stream

# mngr replays rotated files named ``events.jsonl.<digits>``; an archive must never be one.
_MNGR_ROTATED_STREAM_PATTERN = re.compile(r"^events\.jsonl\.(\d+)$")


def test_a_stream_under_the_threshold_is_left_in_place(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    events_path.write_bytes(b'{"type": "a"}\n')

    assert stage_oversized_stream(events_path, threshold_bytes=64) is False

    assert events_path.read_bytes() == b'{"type": "a"}\n'
    assert sorted(tmp_path.glob("events.jsonl.*")) == []


def test_a_missing_stream_is_not_staged(tmp_path: Path) -> None:
    assert stage_oversized_stream(tmp_path / "events.jsonl", threshold_bytes=0) is False


def test_an_oversized_stream_is_staged_then_archived_under_a_name_mngr_never_replays(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    oversized = b'{"type": "a"}\n' * 10
    events_path.write_bytes(oversized)

    assert (
        stage_oversized_stream(events_path, threshold_bytes=len(oversized) - 1) is True
    )
    assert not events_path.exists()
    (staged,) = sorted(tmp_path.glob("events.jsonl.*"))
    assert staged.name.endswith(".archiving")

    compress_staged_streams(events_path, kept_archive_count=3)

    (archive,) = sorted(tmp_path.glob("events.jsonl.*"))
    assert archive.name.endswith(".gz")
    assert gzip.decompress(archive.read_bytes()) == oversized
    assert _MNGR_ROTATED_STREAM_PATTERN.match(archive.name) is None


def test_an_interrupted_archive_is_finished_and_only_the_newest_archives_are_kept(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    for month in range(1, 5):
        (tmp_path / f"events.jsonl.20260{month}01000000000000.gz").write_bytes(
            gzip.compress(b"old\n")
        )
    # Left behind by a program stopped after staging a stream but before compressing it.
    (tmp_path / "events.jsonl.20260501000000000000.archiving").write_bytes(b"staged\n")

    compress_staged_streams(events_path, kept_archive_count=3)

    assert sorted(path.name for path in tmp_path.glob("events.jsonl.*")) == [
        "events.jsonl.20260301000000000000.gz",
        "events.jsonl.20260401000000000000.gz",
        "events.jsonl.20260501000000000000.gz",
    ]
    assert (
        gzip.decompress(
            (tmp_path / "events.jsonl.20260501000000000000.gz").read_bytes()
        )
        == b"staged\n"
    )
