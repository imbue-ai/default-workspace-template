import json
from datetime import datetime
from datetime import timezone

import pytest

from imbue.minds_admin.slices.archive_types import ARCHIVE_BYTES_MARKER
from imbue.minds_admin.slices.archive_types import ARCHIVE_SHA256_MARKER
from imbue.minds_admin.slices.archive_types import ARCHIVE_SUMMARY_MARKER
from imbue.minds_admin.slices.archive_types import ArchiveError
from imbue.minds_admin.slices.archive_types import RetireVerdict
from imbue.minds_admin.slices.archive_types import archive_manifest_key
from imbue.minds_admin.slices.archive_types import archive_object_prefix
from imbue.minds_admin.slices.archive_types import archive_zip_key
from imbue.minds_admin.slices.archive_types import archives_listing_prefix
from imbue.minds_admin.slices.archive_types import classify_retire_verdict
from imbue.minds_admin.slices.archive_types import parse_archive_stream_summary
from imbue.minds_admin.slices.archive_types import parse_archive_upload_output


def test_archive_objects_live_under_the_env_prefix_outside_the_workspace_and_cutover_prefixes() -> None:
    created_at = datetime(2026, 9, 18, 12, 30, 5, tzinfo=timezone.utc)
    prefix = archive_object_prefix("dev-x/", "host-abc", created_at)
    assert prefix == "dev-x/archives/host-abc/20260918T123005Z"
    assert archive_zip_key(prefix) == f"{prefix}/workspace.zip"
    assert archive_manifest_key(prefix) == f"{prefix}/manifest.json"
    assert archives_listing_prefix("dev-x/", None) == "dev-x/archives/"
    assert archives_listing_prefix("", "host-abc") == "archives/host-abc/"
    # The product's release deletes ``<prefix><host_id>/``; the archive must not sit under it.
    assert not prefix.startswith("dev-x/host-abc")


@pytest.mark.parametrize(
    ("status", "baked", "live", "verdict"),
    [
        ("leased", "minds-v0.3.9", "minds-v0.3.9", RetireVerdict.RETIRE),
        ("leased", "minds-v0.3.9", "", RetireVerdict.RETIRE),
        ("leased", "main", "minds-v0.3.10", RetireVerdict.MIGRATE),
        ("leased", "minds-v0.3.9", "minds-v0.5.2", RetireVerdict.MIGRATE),
        ("stopped", "minds-v0.3.10", None, RetireVerdict.MIGRATE),
        ("stopped", "minds-v0.3.9", None, RetireVerdict.UNKNOWN_UNTIL_STARTED),
        ("stopped", "main", None, RetireVerdict.UNKNOWN_UNTIL_STARTED),
        ("stopped", None, None, RetireVerdict.UNKNOWN_UNTIL_STARTED),
        ("leased", "minds-v0.3.9", None, RetireVerdict.UNKNOWN_UNTIL_STARTED),
        ("available", "minds-v0.3.9", None, RetireVerdict.SKIP),
        ("stopping", "minds-v0.3.9", None, RetireVerdict.SKIP),
        ("crashed", "minds-v0.3.9", None, RetireVerdict.SKIP),
    ],
)
def test_classify_retire_verdict_trusts_the_live_probe_over_the_bake(
    status: str, baked: str | None, live: str | None, verdict: RetireVerdict
) -> None:
    # The floor is minds-v0.3.10 inclusive: 0.3.9 retires, 0.3.10 migrates,
    # and a probe that names no release tag retires too.
    found, reason = classify_retire_verdict(status, baked, live)
    assert found == verdict
    assert reason


def test_parse_archive_upload_output_reads_the_markers_and_refuses_an_empty_upload() -> None:
    sha = "ab" * 32
    stdout = f"noise\n{ARCHIVE_SHA256_MARKER} {sha}\n{ARCHIVE_BYTES_MARKER} 4096\n"
    assert parse_archive_upload_output(stdout) == (sha, 4096)
    with pytest.raises(ArchiveError, match="sha256"):
        parse_archive_upload_output(f"{ARCHIVE_BYTES_MARKER} 4096\n")
    with pytest.raises(ArchiveError, match="sha256"):
        parse_archive_upload_output(f"{ARCHIVE_SHA256_MARKER} nothex\n{ARCHIVE_BYTES_MARKER} 4096\n")
    with pytest.raises(ArchiveError, match="empty upload"):
        parse_archive_upload_output(f"{ARCHIVE_SHA256_MARKER} {sha}\n{ARCHIVE_BYTES_MARKER} 0\n")


def test_parse_archive_stream_summary_finds_the_last_marker_among_other_stderr() -> None:
    payload = {"entry_count": 3, "entry_bytes": 10, "skipped_count": 1, "skipped_paths": ["a/b: fifo"]}
    stderr = f"Warning: Permanently added ...\n{ARCHIVE_SUMMARY_MARKER} {json.dumps(payload)}\n"
    summary = parse_archive_stream_summary(stderr)
    assert summary.entry_count == 3
    assert summary.skipped_paths == ("a/b: fifo",)
    with pytest.raises(ArchiveError, match="no summary line"):
        parse_archive_stream_summary("only ssh noise\n")
    with pytest.raises(ArchiveError, match="not JSON"):
        parse_archive_stream_summary(f"{ARCHIVE_SUMMARY_MARKER} {{broken\n")
