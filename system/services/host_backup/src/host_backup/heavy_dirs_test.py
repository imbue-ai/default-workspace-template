"""Unit tests for host_backup.heavy_dirs (listing parsing and selection; no restic)."""

from __future__ import annotations

import json

from host_backup.heavy_dirs import count_snapshot_listing, select_heavy_directories


def _listing(*paths: str) -> list[str]:
    snapshot = {
        "struct_type": "snapshot",
        "message_type": "snapshot",
        "short_id": "1a2b3c4d",
        "time": "2026-09-30T21:32:00Z",
    }
    nodes = [
        {"struct_type": "node", "message_type": "node", "path": path, "type": "file"}
        for path in paths
    ]
    return [json.dumps(record) + "\n" for record in (snapshot, *nodes)]


def _tarball_cache_listing() -> list[str]:
    """A workspace whose app cache holds most entries, beside a little real data."""
    cache_files = [
        f"/workspace/data/.apps/pr-review/repos/repo-a/sha-{tree}/file-{index}"
        for tree in range(3)
        for index in range(30)
    ]
    other_files = [f"/workspace/data/notes/note-{index}" for index in range(6)]
    return _listing(*cache_files, *other_files, "/.bashrc") + ["not json at all\n"]


def test_count_snapshot_listing_credits_each_entry_to_every_ancestor() -> None:
    counts = count_snapshot_listing(_tarball_cache_listing(), max_depth=8)

    assert counts.snapshot_short_id == "1a2b3c4d"
    assert counts.entry_count_by_directory["/"] == 97
    assert counts.entry_count_by_directory["/workspace/data"] == 96
    assert counts.entry_count_by_directory["/workspace/data/.apps/pr-review/repos"] == 90
    assert counts.entry_count_by_directory["/workspace/data/.apps/pr-review/repos/repo-a/sha-1"] == 30
    assert counts.entry_count_by_directory["/workspace/data/notes"] == 6


def test_count_snapshot_listing_stops_counting_below_the_depth_limit() -> None:
    counts = count_snapshot_listing(_tarball_cache_listing(), max_depth=3)

    assert counts.entry_count_by_directory["/workspace/data/.apps"] == 90
    assert "/workspace/data/.apps/pr-review" not in counts.entry_count_by_directory


def test_select_heavy_directories_follows_the_chain_down_to_the_heavy_directory() -> None:
    counts = count_snapshot_listing(_tarball_cache_listing(), max_depth=8)

    heavy = select_heavy_directories(counts, min_share=0.2)

    assert [(directory.path, directory.entry_count, directory.depth) for directory in heavy] == [
        ("/workspace", 96, 1),
        ("/workspace/data", 96, 2),
        ("/workspace/data/.apps", 90, 3),
        ("/workspace/data/.apps/pr-review", 90, 4),
        ("/workspace/data/.apps/pr-review/repos", 90, 5),
        ("/workspace/data/.apps/pr-review/repos/repo-a", 90, 6),
        ("/workspace/data/.apps/pr-review/repos/repo-a/sha-0", 30, 7),
        ("/workspace/data/.apps/pr-review/repos/repo-a/sha-1", 30, 7),
        ("/workspace/data/.apps/pr-review/repos/repo-a/sha-2", 30, 7),
    ]


def test_select_heavy_directories_lists_the_heavier_sibling_first() -> None:
    counts = count_snapshot_listing(
        _listing(*[f"/light/f-{index}" for index in range(3)], *[f"/heavy/f-{index}" for index in range(7)]),
        max_depth=8,
    )

    heavy = select_heavy_directories(counts, min_share=0.1)

    assert [directory.path for directory in heavy] == ["/heavy", "/light"]


def test_select_heavy_directories_is_empty_for_an_empty_snapshot() -> None:
    counts = count_snapshot_listing(_listing(), max_depth=8)

    assert select_heavy_directories(counts, min_share=0.02) == []
