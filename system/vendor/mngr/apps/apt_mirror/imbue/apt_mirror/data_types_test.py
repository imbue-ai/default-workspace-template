import pytest
from pydantic import ValidationError

from imbue.apt_mirror.data_types import ArchiveSource
from imbue.apt_mirror.data_types import DEFAULT_ARCHIVES


def test_archive_source_requires_at_least_one_component() -> None:
    # required_component is components[0]; an archive with none must fail at
    # construction rather than during resolution.
    with pytest.raises(ValidationError):
        ArchiveSource(name="empty", live_base="https://x", snapshot_base=None, suites=("trixie",), components=())
    for archive in DEFAULT_ARCHIVES:
        assert archive.required_component == archive.components[0]


def test_archive_source_snapshot_root_is_the_frozen_archive_at_the_timestamp_or_none() -> None:
    debian, security, docker = DEFAULT_ARCHIVES
    assert debian.snapshot_root("20260725T000000Z") == "https://snapshot.debian.org/archive/debian/20260725T000000Z"
    assert security.snapshot_root("20260725T000000Z") == (
        "https://snapshot.debian.org/archive/debian-security/20260725T000000Z"
    )
    assert docker.snapshot_root("20260725T000000Z") is None
