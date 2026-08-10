from pathlib import Path

import pytest

from share_gateway.grants import GrantsError
from share_gateway.grants import load_grants
from share_gateway.grants import parse_grants

_FULL_GRANTS = """
[workspace]
emails = ["Bob@Example.com"]
email_domains = ["imbue.com"]

[services.web]
emails = ["carol@example.com"]
email_domains = ["partner.org"]
"""


def test_workspace_grant_admits_every_service_and_the_shell() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows("bob@example.com", None) is True
    assert grants.allows("bob@example.com", "web") is True
    assert grants.allows("bob@example.com", "terminal") is True


def test_workspace_email_domain_grant_matches_by_domain() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows("anyone@imbue.com", None) is True
    assert grants.allows("anyone@not-imbue.com", None) is False
    assert grants.allows("imbue.com", None) is False


def test_per_service_grant_admits_only_that_service() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows("carol@example.com", "web") is True
    assert grants.allows("carol@example.com", None) is False
    assert grants.allows("carol@example.com", "terminal") is False
    assert grants.allows("dave@partner.org", "web") is True
    assert grants.allows("dave@partner.org", "terminal") is False


def test_grant_matching_is_case_insensitive() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows("BOB@EXAMPLE.COM", None) is True
    assert grants.allows("Anyone@IMBUE.com", None) is True


def test_allows_any_covers_workspace_and_service_grants() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows_any("bob@example.com") is True
    assert grants.allows_any("carol@example.com") is True
    assert grants.allows_any("stranger@nowhere.dev") is False


def test_empty_grants_admit_nobody() -> None:
    grants = parse_grants("")
    assert grants.allows("anyone@example.com", None) is False
    assert grants.allows_any("anyone@example.com") is False


@pytest.mark.parametrize(
    "text",
    [
        "not toml [[",
        "[workspace]\nemails = 'not-a-list'",
        "[workspace]\nemails = [1, 2]",
        "[workspace]\nemail_domains = 3",
        "workspace = 'not-a-table'",
        "[services]\nweb = 'not-a-table'",
    ],
)
def test_malformed_grants_raise(text: str) -> None:
    with pytest.raises(GrantsError):
        parse_grants(text)


def test_load_grants_fails_closed_when_file_missing(tmp_path: Path) -> None:
    with pytest.raises(GrantsError):
        load_grants(tmp_path / "absent.toml")


def test_load_grants_reads_file(tmp_path: Path) -> None:
    grants_path = tmp_path / "share_grants.toml"
    grants_path.write_text(_FULL_GRANTS)
    grants = load_grants(grants_path)
    assert grants.allows("bob@example.com", None) is True
