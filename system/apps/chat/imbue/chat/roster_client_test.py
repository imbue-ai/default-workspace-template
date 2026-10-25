from workspace_layout.errors import ShellUnreachableError
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import ParticipantKey
from workspace_layout.roster import RosterDocument
from workspace_layout.testing import FakeShell
from workspace_layout.testing import roster_participant

from imbue.chat.roster_client import RosterClient
from imbue.chat.roster_client import fallback_display_name

_BOB = ParticipantKey("user:user-bob-4471")


def _roster(*names: str) -> RosterDocument:
    return RosterDocument(
        participants=tuple(
            roster_participant(
                _BOB if name == "Bob" else ParticipantKey(f"user:user-{name.lower()}"), display_name=name
            )
            for name in names
        )
    )


def test_the_roster_is_read_from_the_shell_and_cached_until_it_ages() -> None:
    shell = FakeShell(roster_document=_roster("Bob"))
    now = [100.0]
    client = RosterClient(shell=shell, cache_seconds=5.0, clock=lambda: now[0])

    assert client.participant(_BOB) is not None
    shell.roster_document = _roster("Robert")
    now[0] = 104.0
    assert client.display_name_of(_BOB, RequestIdentity(owner=False, user_id="user-bob-4471", email="b@x")) == "Bob"
    now[0] = 106.0
    assert client.participant(_BOB) is None
    assert client.participant(ParticipantKey("user:user-robert")) is not None


def test_an_unreachable_shell_keeps_the_last_roster_and_names_a_sender_from_their_header() -> None:
    shell = FakeShell(roster_document=_roster("Bob"))
    now = [0.0]
    client = RosterClient(shell=shell, cache_seconds=5.0, clock=lambda: now[0])
    assert client.participant(_BOB) is not None

    shell.listing_error = ShellUnreachableError("down")
    now[0] = 10.0
    assert client.participant(_BOB) is not None
    unknown = ParticipantKey("user:user-carol-1138")
    assert (
        client.display_name_of(
            unknown, RequestIdentity(owner=False, user_id="user-carol-1138", email="carol@example.com")
        )
        == "carol"
    )

    fresh = RosterClient(shell=FakeShell(listing_error=ShellUnreachableError("down")))
    assert fresh.document().participants == ()


def test_the_fallback_name_prefers_an_agents_grant_name_then_the_emails_local_part() -> None:
    agent = RequestIdentity(owner=False, agent_id="va-1", agent_name="Josh's Claude", granter="x")
    assert fallback_display_name(ParticipantKey("agent:va-1"), agent) == "Josh's Claude"
    assert fallback_display_name(_BOB, RequestIdentity(owner=False, user_id="user-bob-4471", email="@nowhere")) == str(
        _BOB
    )


def test_every_fresh_roster_is_told_of_and_a_cached_read_is_not() -> None:
    shell = FakeShell(roster_document=_roster("Bob"))
    now = [100.0]
    told: list[RosterDocument] = []
    client = RosterClient(shell=shell, cache_seconds=5.0, clock=lambda: now[0], on_refreshed=told.append)

    client.document()
    client.document()
    now[0] = 106.0
    shell.roster_document = _roster("Bob", "Carol")
    client.document()
    shell.listing_error = ShellUnreachableError("down")
    now[0] = 112.0
    client.document()

    assert [[entry.display_name for entry in document.participants] for document in told] == [
        ["Bob"],
        ["Bob", "Carol"],
    ]
