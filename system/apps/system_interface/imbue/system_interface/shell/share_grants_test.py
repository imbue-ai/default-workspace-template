"""Tests for the readers over the share materials the minds desktop writes."""

import json
import os
from pathlib import Path

from imbue.system_interface.shell.data_types import GrantedIdentity
from imbue.system_interface.shell.share_grants import ShareGrantsReader
from imbue.system_interface.shell.share_grants import ShareIdentitiesReader

# The document as the minds desktop renders it: every scope carries all three lists.
_GRANTS_WITH_A_PER_APP_GRANT = """\
[workspace]
users = ["3f1c"]
emails = []
email_domains = []

[services.docs]
users = []
emails = ["reviewer@example.com"]
email_domains = []

[services.notes]
users = []
emails = []
email_domains = []
"""


def _reader_over(tmp_path: Path) -> ShareGrantsReader:
    return ShareGrantsReader(share_env_path=tmp_path / "share.env", share_grants_path=tmp_path / "share_grants.toml")


def test_the_reader_grants_no_app_while_the_workspace_is_not_shared(tmp_path: Path) -> None:
    reader = _reader_over(tmp_path)
    reader.share_grants_path.write_text(_GRANTS_WITH_A_PER_APP_GRANT)

    assert reader.granted_app_names() == set()

    reader.share_env_path.write_text("export SHARE_BROKER_URL=https://broker.example\n")
    assert reader.granted_app_names() == {"docs"}


def test_the_reader_grants_no_app_while_the_document_is_missing(tmp_path: Path, loguru_records: list[str]) -> None:
    reader = _reader_over(tmp_path)
    reader.share_env_path.write_text("")

    assert reader.granted_app_names() == set()
    assert [record for record in loguru_records if record.startswith("WARNING")] == []


def test_the_reader_warns_once_per_version_of_a_document_the_gateway_would_refuse(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    reader = _reader_over(tmp_path)
    reader.share_env_path.write_text("")
    reader.share_grants_path.write_text("[services\n")

    assert reader.granted_app_names() == set()
    assert reader.granted_app_names() == set()
    warnings = [record for record in loguru_records if record.startswith("WARNING")]
    assert len(warnings) == 1
    assert "not valid TOML" in warnings[0]

    # A rewrite that is still broken is warned about again; a good one is read.
    reader.share_grants_path.write_text("[services.docs]\nusers = 3\n")
    _bump_mtime(reader.share_grants_path)
    assert reader.granted_app_names() == set()
    assert len([record for record in loguru_records if record.startswith("WARNING")]) == 2
    reader.share_grants_path.write_text(_GRANTS_WITH_A_PER_APP_GRANT)
    assert reader.granted_app_names() == {"docs"}


def _bump_mtime(path: Path) -> None:
    """Move a file's mtime forward by a second, so a rewrite within the clock's resolution still reads as a new
    version."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


def test_the_identities_reader_answers_what_the_desktop_recorded_and_nothing_while_the_file_is_absent(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    reader = ShareIdentitiesReader(share_identities_path=tmp_path / "share_identities.json")

    assert reader.read_identities() == {}

    reader.share_identities_path.write_text(
        json.dumps(
            {
                "version": 1,
                "identities": {
                    "3f1c": {"email": "alice@example.com", "display_name": "Alice", "profile_picture_url": "x"},
                    "9a7b": {"email": None, "display_name": None},
                },
            }
        )
    )
    assert reader.read_identities() == {
        "3f1c": GrantedIdentity(email="alice@example.com", display_name="Alice"),
        "9a7b": GrantedIdentity(email=None, display_name=None),
    }
    assert [record for record in loguru_records if record.startswith("WARNING")] == []


def test_the_identities_reader_warns_once_per_version_of_a_file_it_cannot_read(
    tmp_path: Path, loguru_records: list[str]
) -> None:
    reader = ShareIdentitiesReader(share_identities_path=tmp_path / "share_identities.json")
    reader.share_identities_path.write_text(json.dumps({"version": 1, "identities": {"3f1c": "alice"}}))

    assert reader.read_identities() == {}
    assert reader.read_identities() == {}
    warnings = [record for record in loguru_records if record.startswith("WARNING")]
    assert len(warnings) == 1
    assert "share_identities.json" in warnings[0]

    # A version this shell does not know is warned about once too; a good rewrite is read.
    reader.share_identities_path.write_text(json.dumps({"version": 2, "identities": {}}))
    _bump_mtime(reader.share_identities_path)
    assert reader.read_identities() == {}
    assert reader.read_identities() == {}
    assert len([record for record in loguru_records if record.startswith("WARNING")]) == 2
    reader.share_identities_path.write_text(json.dumps({"version": 1, "identities": {}}))
    _bump_mtime(reader.share_identities_path)
    assert reader.read_identities() == {}
    assert len([record for record in loguru_records if record.startswith("WARNING")]) == 2

    # So is one that is not JSON at all, or not a JSON object.
    reader.share_identities_path.write_text("{not json")
    _bump_mtime(reader.share_identities_path)
    assert reader.read_identities() == {}
    assert reader.read_identities() == {}
    assert len([record for record in loguru_records if record.startswith("WARNING")]) == 3
    reader.share_identities_path.write_text("[]")
    _bump_mtime(reader.share_identities_path)
    assert reader.read_identities() == {}
    assert reader.read_identities() == {}
    assert len([record for record in loguru_records if record.startswith("WARNING")]) == 4
