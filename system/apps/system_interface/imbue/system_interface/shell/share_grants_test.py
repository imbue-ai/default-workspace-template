import os
from pathlib import Path

import pytest

from imbue.system_interface.shell.errors import ShareGrantsError
from imbue.system_interface.shell.share_grants import ShareGrantsReader
from imbue.system_interface.shell.share_grants import parse_granted_app_names

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


def test_parse_granted_app_names_answers_the_apps_whose_table_names_anyone() -> None:
    assert parse_granted_app_names(_GRANTS_WITH_A_PER_APP_GRANT) == {"docs"}


@pytest.mark.parametrize(
    "grants_text",
    [
        "",
        '[workspace]\nusers = ["3f1c"]\nemails = ["friend@example.com"]\nemail_domains = ["example.org"]\n',
        '[services.docs]\nusers = ["  ", ""]\n',
        '[services."my-app"]\n',
    ],
)
def test_parse_granted_app_names_grants_no_app_for_a_workspace_grant_alone_or_an_empty_table(
    grants_text: str,
) -> None:
    assert parse_granted_app_names(grants_text) == set()


def test_parse_granted_app_names_reads_a_quoted_table_key_and_each_of_the_three_lists() -> None:
    text = '[services."my-app"]\nemail_domains = ["example.org"]\n\n[services.other]\nusers = ["3f1c"]\n'
    assert parse_granted_app_names(text) == {"my-app", "other"}


@pytest.mark.parametrize(
    ("grants_text", "reason"),
    [
        ("[services\n", "not valid TOML"),
        ("services = 1\n", "must be a table of per-app tables"),
        ("[services]\ndocs = 1\n", "must be a table"),
        ('[services.docs]\nusers = "3f1c"\n', "must be a list of strings"),
        ("[services.docs]\nemails = [1]\n", "must be a list of strings"),
        # Every list of a table is checked, not only the first that names someone.
        ('[services.docs]\nusers = ["3f1c"]\nemails = 1\n', "scope 'services.docs': emails must be a list"),
        # The workspace table is checked too: a malformed one makes the gateway admit nobody.
        ('workspace = 1\n\n[services.docs]\nusers = ["3f1c"]\n', "scope 'workspace' must be a table"),
        ('[workspace]\nusers = "3f1c"\n\n[services.docs]\nusers = ["3f1c"]\n', "scope 'workspace': users must be"),
    ],
)
def test_parse_granted_app_names_refuses_what_the_gateway_refuses(grants_text: str, reason: str) -> None:
    with pytest.raises(ShareGrantsError, match=reason):
        parse_granted_app_names(grants_text)


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
    """Move a file's mtime forward by a second, so a rewrite within the clock's resolution still reads as a new version."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
