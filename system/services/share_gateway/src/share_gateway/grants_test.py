import threading
from pathlib import Path

import pytest

from share_gateway.grants import GrantsError
from share_gateway.grants import grants_lock_path
from share_gateway.grants import load_grants
from share_gateway.grants import locked_grants_file
from share_gateway.grants import parse_grants
from share_gateway.grants import render_grants
from share_gateway.grants import upgrade_invites
from share_gateway.grants import upgrade_invites_in_file

_FULL_GRANTS = """
[workspace]
users = ["user-erin-0001"]
emails = ["Bob@Example.com"]
email_domains = ["imbue.com"]

[services.web]
users = []
emails = ["carol@example.com"]
email_domains = ["partner.org"]
"""

# Documents written before ``users`` existed carry only the two email lists.
_LEGACY_GRANTS = """
[workspace]
emails = ["bob@example.com"]
email_domains = []
"""

_NOBODY = "user-nobody-0000"


def test_workspace_grant_admits_every_service_and_the_shell() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows(_NOBODY, "bob@example.com", None) is True
    assert grants.allows(_NOBODY, "bob@example.com", "web") is True
    assert grants.allows(_NOBODY, "bob@example.com", "terminal") is True


def test_user_id_grant_matches_regardless_of_email() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows("user-erin-0001", "renamed@elsewhere.dev", None) is True
    assert grants.allows("user-erin-0001", "renamed@elsewhere.dev", "terminal") is True
    assert grants.allows("user-other-0002", "renamed@elsewhere.dev", None) is False


def test_workspace_email_domain_grant_matches_by_domain() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows(_NOBODY, "anyone@imbue.com", None) is True
    assert grants.allows(_NOBODY, "anyone@not-imbue.com", None) is False
    assert grants.allows(_NOBODY, "imbue.com", None) is False


def test_per_service_grant_admits_only_that_service() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows(_NOBODY, "carol@example.com", "web") is True
    assert grants.allows(_NOBODY, "carol@example.com", None) is False
    assert grants.allows(_NOBODY, "carol@example.com", "terminal") is False
    assert grants.allows(_NOBODY, "dave@partner.org", "web") is True
    assert grants.allows(_NOBODY, "dave@partner.org", "terminal") is False


def test_grant_matching_is_case_insensitive() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows(_NOBODY, "BOB@EXAMPLE.COM", None) is True
    assert grants.allows(_NOBODY, "Anyone@IMBUE.com", None) is True


def test_allows_any_covers_workspace_and_service_grants() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.allows_any(_NOBODY, "bob@example.com") is True
    assert grants.allows_any(_NOBODY, "carol@example.com") is True
    assert grants.allows_any("user-erin-0001", "stranger@nowhere.dev") is True
    assert grants.allows_any(_NOBODY, "stranger@nowhere.dev") is False


def test_a_document_without_users_lists_still_parses() -> None:
    grants = parse_grants(_LEGACY_GRANTS)
    assert grants.allows(_NOBODY, "bob@example.com", None) is True
    assert grants.workspace.users == set()


def test_empty_grants_admit_nobody() -> None:
    grants = parse_grants("")
    assert grants.allows(_NOBODY, "anyone@example.com", None) is False
    assert grants.allows_any(_NOBODY, "anyone@example.com") is False


def test_has_email_invite_is_exact_and_ignores_domain_grants() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert grants.has_email_invite("bob@example.com") is True
    assert grants.has_email_invite("CAROL@example.com") is True
    assert grants.has_email_invite("anyone@imbue.com") is False
    assert grants.has_email_invite("erin@example.com") is False


def test_render_grants_roundtrips_through_parse() -> None:
    rendered = render_grants(parse_grants(_FULL_GRANTS))
    reparsed = parse_grants(rendered)
    assert reparsed.workspace.users == {"user-erin-0001"}
    assert reparsed.workspace.emails == {"bob@example.com"}
    assert reparsed.workspace.email_domains == {"imbue.com"}
    assert reparsed.services["web"].emails == {"carol@example.com"}
    assert reparsed.services["web"].email_domains == {"partner.org"}
    assert rendered == render_grants(reparsed)


def test_render_grants_quotes_awkward_service_names_and_values() -> None:
    grants = parse_grants('[services."my app"]\nemails = ["a\\"b@example.com"]\n')
    reparsed = parse_grants(render_grants(grants))
    assert reparsed.services["my app"].emails == {'a"b@example.com'}


def test_upgrade_invites_moves_the_email_to_a_user_grant_in_every_scope() -> None:
    grants = parse_grants(
        '[workspace]\nemails = ["bob@example.com"]\n[services.web]\nemails = ["Bob@Example.com", "carol@example.com"]\n'
    )
    upgraded = upgrade_invites(grants, "BOB@example.com", "user-bob-4471")
    assert upgraded is not None
    assert upgraded.workspace.users == {"user-bob-4471"}
    assert upgraded.workspace.emails == set()
    assert upgraded.services["web"].users == {"user-bob-4471"}
    assert upgraded.services["web"].emails == {"carol@example.com"}


def test_upgrade_invites_is_none_when_the_email_is_not_invited() -> None:
    grants = parse_grants(_FULL_GRANTS)
    assert upgrade_invites(grants, "anyone@imbue.com", "user-anyone-1") is None
    assert upgrade_invites(grants, "stranger@nowhere.dev", "user-stranger-1") is None


def test_upgrade_invites_in_file_rewrites_atomically_under_the_lock(tmp_path: Path) -> None:
    grants_path = tmp_path / "share_grants.toml"
    grants_path.write_text(_FULL_GRANTS)

    assert upgrade_invites_in_file(grants_path, "bob@example.com", "user-bob-4471") is True

    reparsed = load_grants(grants_path)
    assert reparsed.workspace.users == {"user-bob-4471", "user-erin-0001"}
    assert reparsed.workspace.emails == set()
    assert reparsed.services["web"].emails == {"carol@example.com"}
    assert grants_lock_path(grants_path).exists()
    # No temp file survives the rename.
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["share_grants.toml", "share_grants.toml.lock"]
    assert upgrade_invites_in_file(grants_path, "bob@example.com", "user-bob-4471") is False


def test_upgrade_invites_in_file_waits_for_a_concurrent_writer(tmp_path: Path) -> None:
    grants_path = tmp_path / "share_grants.toml"
    grants_path.write_text('[workspace]\nemails = ["bob@example.com", "carol@example.com"]\n')
    holder_ready = threading.Event()
    release_holder = threading.Event()
    upgrade_finished = threading.Event()

    def hold_lock_and_rewrite() -> None:
        with locked_grants_file(grants_path):
            holder_ready.set()
            release_holder.wait(timeout=10)
            grants_path.write_text('[workspace]\nemails = ["bob@example.com"]\n')

    def upgrade() -> None:
        upgrade_invites_in_file(grants_path, "bob@example.com", "user-bob-4471")
        upgrade_finished.set()

    holder = threading.Thread(target=hold_lock_and_rewrite)
    upgrader = threading.Thread(target=upgrade)
    holder.start()
    assert holder_ready.wait(timeout=10)
    upgrader.start()
    # The upgrade cannot land while the other writer holds the lock ...
    assert upgrade_finished.wait(timeout=0.5) is False
    release_holder.set()
    holder.join(timeout=10)
    upgrader.join(timeout=10)
    assert upgrade_finished.is_set()
    # ... and when it does, it sees the writer's document, not the one it was called against.
    reparsed = load_grants(grants_path)
    assert reparsed.workspace.users == {"user-bob-4471"}
    assert reparsed.workspace.emails == set()


def test_upgrade_invites_in_file_raises_when_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(GrantsError):
        upgrade_invites_in_file(tmp_path / "absent.toml", "bob@example.com", "user-bob-4471")


@pytest.mark.parametrize(
    "text",
    [
        "not toml [[",
        "[workspace]\nemails = 'not-a-list'",
        "[workspace]\nemails = [1, 2]",
        "[workspace]\nusers = 'not-a-list'",
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
    assert grants.allows(_NOBODY, "bob@example.com", None) is True
