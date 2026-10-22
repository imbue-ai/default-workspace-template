"""Tests for the per-app share grants: which apps a document grants on their own, and the reader over the share
materials the minds desktop writes."""

import os
from pathlib import Path

import pytest
from share_grants.grants import parse_grants

from imbue.system_interface.shell.share_grants import ShareGrantsReader
from imbue.system_interface.shell.share_grants import granted_app_names_of

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


def test_granted_app_names_are_the_apps_whose_table_names_anyone() -> None:
    assert granted_app_names_of(parse_grants(_GRANTS_WITH_A_PER_APP_GRANT)) == {"docs"}


@pytest.mark.parametrize(
    "grants_text",
    [
        "",
        '[workspace]\nusers = ["3f1c"]\nemails = ["friend@example.com"]\nemail_domains = ["example.org"]\n',
        '[services.docs]\nusers = ["  ", ""]\n',
        '[services."my-app"]\n',
    ],
)
def test_no_app_is_granted_by_a_workspace_grant_alone_or_an_empty_table(grants_text: str) -> None:
    assert granted_app_names_of(parse_grants(grants_text)) == set()


def test_a_quoted_table_key_and_each_of_the_three_lists_grant_their_app() -> None:
    text = '[services."my-app"]\nemail_domains = ["example.org"]\n\n[services.other]\nusers = ["3f1c"]\n'
    assert granted_app_names_of(parse_grants(text)) == {"my-app", "other"}


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
