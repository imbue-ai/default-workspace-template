import json
import stat
from pathlib import Path

import pytest

from share_grants.document import (
    SHARE_ENV_RELATIVE_PATH,
    SHARE_GRANTS_RELATIVE_PATH,
    SHARE_IDENTITIES_RELATIVE_PATH,
    GrantedIdentityEntry,
    GrantsEntries,
    GrantShapeRefusal,
    grant_entries,
    grant_shape_refusals,
    normalized_grants,
    read_sharing_state,
    replace_grants,
)
from share_grants.grants import load_grants, parse_grants

_NO_ENTRIES = grant_entries((), (), ())


def _document(users: tuple[str, ...], emails: tuple[str, ...], email_domains: tuple[str, ...]) -> GrantsEntries:
    return GrantsEntries(workspace=grant_entries(users, emails, email_domains), services={})


def test_a_document_of_well_shaped_entries_is_not_refused() -> None:
    document = GrantsEntries(
        workspace=grant_entries(("user-1",), ("Bob@Example.com",), ("@Example.org",)),
        services={"web": grant_entries((), ("carol@münchen.de",), ())},
    )

    assert grant_shape_refusals(document, None) == []


def test_each_kind_of_misshapen_entry_is_refused_with_the_entry_as_sent() -> None:
    document = GrantsEntries(
        workspace=grant_entries(("", "user-1", "user-1"), ("not an address", " A@b.co ", "a@b.co"), ("nodot",)),
        services={"web": grant_entries((), (), ("example.com", "@EXAMPLE.com"))},
    )

    assert grant_shape_refusals(document, None) == [
        GrantShapeRefusal("workspace", "user", "", "This entry is empty."),
        GrantShapeRefusal("workspace", "user", "user-1", "user-1 is already granted here."),
        GrantShapeRefusal("workspace", "email", "not an address", "not an address is not an email address."),
        GrantShapeRefusal("workspace", "email", "a@b.co", "a@b.co is already granted here."),
        GrantShapeRefusal("workspace", "email_domain", "nodot", "nodot is not a domain."),
        GrantShapeRefusal("web", "email_domain", "@EXAMPLE.com", "example.com is already granted here."),
    ]


def test_the_value_of_an_entry_the_stored_document_holds_is_not_checked() -> None:
    stored = parse_grants('[workspace]\nusers = []\nemails = ["legacy-entry"]\nemail_domains = ["intranet"]\n')
    document = _document((), ("legacy-entry", "new-entry"), ("intranet", "other"))

    assert grant_shape_refusals(document, stored) == [
        GrantShapeRefusal("workspace", "email", "new-entry", "new-entry is not an email address."),
        GrantShapeRefusal("workspace", "email_domain", "other", "other is not a domain."),
    ]


def test_a_stored_entry_sent_twice_is_still_a_duplicate() -> None:
    stored = parse_grants('[workspace]\nusers = []\nemails = ["bob@example.com"]\nemail_domains = []\n')
    document = _document((), ("bob@example.com", "Bob@example.com"), ())

    assert grant_shape_refusals(document, stored) == [
        GrantShapeRefusal("workspace", "email", "Bob@example.com", "bob@example.com is already granted here."),
    ]


def test_normalized_grants_trims_lowercases_and_drops_a_domains_at_sign() -> None:
    grants = normalized_grants(
        GrantsEntries(
            workspace=grant_entries((" User-A ",), (" Bob@Example.COM",), ("@Example.org",)),
            services={"web": grant_entries((), ("carol@example.com",), ())},
        )
    )

    assert grants.workspace.users == {"User-A"}
    assert grants.workspace.emails == {"bob@example.com"}
    assert grants.workspace.email_domains == {"example.org"}
    assert grants.services["web"].emails == {"carol@example.com"}


def test_read_sharing_state_of_a_workspace_that_never_shared(tmp_path: Path) -> None:
    state = read_sharing_state(tmp_path)

    assert state.is_shared is False
    assert state.grants is None
    assert state.is_grants_unreadable is False


def test_read_sharing_state_reads_the_grants_and_whether_share_env_exists(tmp_path: Path) -> None:
    (tmp_path / SHARE_ENV_RELATIVE_PATH).parent.mkdir(parents=True)
    (tmp_path / SHARE_ENV_RELATIVE_PATH).write_text("export SHARE_WORKSPACE_DOMAIN=x\n")
    (tmp_path / SHARE_GRANTS_RELATIVE_PATH).write_text(
        '[workspace]\nusers = ["user-1"]\nemails = []\nemail_domains = []\n'
    )

    state = read_sharing_state(tmp_path)

    assert state.is_shared is True
    assert state.grants is not None
    assert state.grants.workspace.users == {"user-1"}


def test_a_grants_file_that_is_not_a_grants_document_reads_as_none_so_a_save_replaces_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / SHARE_GRANTS_RELATIVE_PATH).parent.mkdir(parents=True)
    (tmp_path / SHARE_GRANTS_RELATIVE_PATH).write_text("this is = not [ toml")

    state = read_sharing_state(tmp_path)

    assert state.grants is None
    assert state.is_grants_unreadable is False
    assert "is not a grants document" in caplog.text


def test_a_grants_file_that_cannot_be_read_is_reported_and_not_read_as_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A directory where the file belongs: there, and unreadable as a file by any user.
    (tmp_path / SHARE_GRANTS_RELATIVE_PATH).mkdir(parents=True)

    state = read_sharing_state(tmp_path)

    assert state.grants is None
    assert state.is_grants_unreadable is True
    assert "Could not read the grants file" in caplog.text


def test_replace_grants_writes_the_identities_and_the_grants_for_their_owner_alone(tmp_path: Path) -> None:
    grants = normalized_grants(_document(("user-1",), ("bob@example.com",), ()))

    replace_grants(tmp_path, grants, {"user-1": GrantedIdentityEntry(email="erin@example.com", display_name="Erin")})

    stored = load_grants(tmp_path / SHARE_GRANTS_RELATIVE_PATH)
    assert stored.workspace.users == {"user-1"}
    assert stored.workspace.emails == {"bob@example.com"}
    identities = json.loads((tmp_path / SHARE_IDENTITIES_RELATIVE_PATH).read_text())
    assert identities == {
        "version": 1,
        "identities": {"user-1": {"email": "erin@example.com", "display_name": "Erin"}},
    }
    for relative_path in (SHARE_GRANTS_RELATIVE_PATH, SHARE_IDENTITIES_RELATIVE_PATH):
        assert stat.S_IMODE((tmp_path / relative_path).stat().st_mode) == 0o600


def test_replace_grants_replaces_the_whole_document(tmp_path: Path) -> None:
    replace_grants(tmp_path, normalized_grants(_document(("user-1",), (), ("example.org",))), {})

    replace_grants(tmp_path, normalized_grants(GrantsEntries(workspace=_NO_ENTRIES, services={})), {})

    stored = load_grants(tmp_path / SHARE_GRANTS_RELATIVE_PATH)
    assert stored.workspace.users == set()
    assert stored.workspace.email_domains == set()
    assert json.loads((tmp_path / SHARE_IDENTITIES_RELATIVE_PATH).read_text())["identities"] == {}
