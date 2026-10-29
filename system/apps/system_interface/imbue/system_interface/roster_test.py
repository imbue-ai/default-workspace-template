import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
from workspace_layout.identity import RequestIdentity
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import UserId
from workspace_layout.roster import PARTICIPANT_COLORS
from workspace_layout.roster import RosterDocument

from imbue.imbue_common.model_update import to_update
from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.avatar.selection import AvatarSelectionStore
from imbue.system_interface.presence import PRESENCE_CONNECTED_WINDOW
from imbue.system_interface.presence import PresenceStore
from imbue.system_interface.profiles import ProfileResolver
from imbue.system_interface.profiles import picture_version_of
from imbue.system_interface.roster import RosterSource
from imbue.system_interface.roster import RosterWatch
from imbue.system_interface.shell.data_types import UserRecord
from imbue.system_interface.shell.share_grants import AgentGrantsReader
from imbue.system_interface.shell.share_grants import ShareGrantsReader
from imbue.system_interface.shell.users import UserStore
from imbue.system_interface.workspace_name import workspace_name_source

_T0 = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)
_BOB = RequestIdentity(owner=False, user_id="user-bob-4471", email="bob@example.com")
_OWNER = RequestIdentity(owner=True, user_id="user-owner-9c21", email="owner@example.com")
_ANONYMOUS_OWNER = RequestIdentity(owner=True)

_GRANTS = """\
[workspace]
users = ["user-bob-4471", "user-carol-1138"]
emails = ["friend@example.com"]
email_domains = ["partner.org"]
"""

_AGENT_GRANTS = """\
[[agents]]
agent_id = "va-0123456789abcdef0123456789abcdef"
name = "Josh's Claude"
public_key = "ssh-ed25519 AAAA"
fingerprint = "SHA256:x"
token_sha256 = "abc"
scope = "workspace"
granter = "user:user-owner-9c21"
created_at = "2026-10-22T09:00:00Z"
"""


def _source(tmp_path: Path) -> RosterSource:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "share.env").write_text("SHARE_BROKER_URL=https://broker.example\n")
    return RosterSource(
        presence=PresenceStore(directory=tmp_path / "presence"),
        profiles=ProfileResolver(cache_directory=tmp_path / "profiles", share_env_path=tmp_path / "no-share.env"),
        users=UserStore(state_directory=tmp_path / "state"),
        share_grants=ShareGrantsReader(
            share_env_path=secrets / "share.env", share_grants_path=secrets / "grants.toml"
        ),
        agent_grants=AgentGrantsReader(agent_grants_path=secrets / "agent_grants.toml"),
        agent_last_seen_path=tmp_path / "state" / "agent_access" / "last_seen.json",
        workspace_name=workspace_name_source("Atlas", {}),
        avatar_selection=AvatarSelectionStore(state_directory=tmp_path / "state"),
    )


def _keys(document: RosterDocument) -> list[str]:
    return [str(entry.key) for entry in document.participants]


def test_an_unshared_workspace_lists_the_anonymous_owner_and_the_workspace_agent(tmp_path: Path) -> None:
    source = _source(tmp_path)
    source.presence.heartbeat(_ANONYMOUS_OWNER, _T0)

    document = source.document(_T0 + timedelta(seconds=5))

    assert _keys(document) == ["owner", "workspace-agent"]
    owner, agent = document.participants
    assert (owner.display_name, owner.is_owner, owner.is_connected, owner.handle) == ("Owner", True, True, "Owner")
    assert owner.kind == "person" and owner.email is None
    assert (agent.display_name, agent.handle, agent.kind) == ("Atlas", "agent", "workspace_agent")
    assert agent.profile_picture_url is not None and agent.profile_picture_url.startswith("/api/avatars/")
    assert agent.is_connected is True


def test_the_owners_key_is_the_synthetic_one_until_their_heartbeats_name_an_account(tmp_path: Path) -> None:
    source = _source(tmp_path)
    source.presence.heartbeat(_BOB, _T0)

    assert str(source.owner_key(_T0)) == "owner"
    source.presence.heartbeat(_OWNER, _T0)
    assert str(source.owner_key(_T0)) == "user:user-owner-9c21"


def test_a_shared_workspace_lists_the_owner_first_then_grants_invited_and_visiting_agents(tmp_path: Path) -> None:
    source = _source(tmp_path)
    source.share_grants.share_grants_path.write_text(_GRANTS)
    source.agent_grants.agent_grants_path.write_text(_AGENT_GRANTS)
    source.agent_last_seen_path.parent.mkdir(parents=True)
    source.agent_last_seen_path.write_text(
        json.dumps({"va-0123456789abcdef0123456789abcdef": (_T0 - timedelta(seconds=20)).isoformat()})
    )
    source.presence.heartbeat(_OWNER, _T0 - timedelta(hours=3))
    source.presence.heartbeat(_BOB, _T0, last_input_at=_T0 - timedelta(seconds=10))
    source.users.record_user(
        UserRecord(
            user_id=UserId("user-carol-1138"),
            desktop_id=DesktopId("carol"),
            desktop_name="Carol",
            email="carol@example.com",
            display_name="Carol",
            last_seen=_T0,
        )
    )

    document = source.document(_T0)

    assert _keys(document) == [
        "user:user-owner-9c21",
        "user:user-bob-4471",
        "user:user-carol-1138",
        "email:friend@example.com",
        "agent:va-0123456789abcdef0123456789abcdef",
        "workspace-agent",
    ]
    owner, bob, carol, friend, claude, agent = document.participants
    # No connector: a person is named after their email's local part, a visitor after what their arrival recorded.
    assert (owner.display_name, owner.is_owner, owner.is_connected, owner.email) == (
        "owner",
        True,
        False,
        "owner@example.com",
    )
    assert owner.last_seen is not None
    assert (bob.display_name, bob.is_connected, bob.last_input_at) == ("bob", True, "2026-10-22T09:59:50.000000000Z")
    assert (carol.display_name, carol.email, carol.is_connected, carol.last_seen) == (
        "Carol",
        "carol@example.com",
        False,
        None,
    )
    assert (friend.kind, friend.display_name, friend.handle, friend.is_connected) == (
        "invited",
        "friend@example.com",
        "friend",
        False,
    )
    assert (claude.kind, claude.display_name, claude.handle, claude.is_connected) == (
        "visiting_agent",
        "Josh's Claude",
        "Joshs-Claude",
        True,
    )
    assert claude.last_seen == "2026-10-22T09:59:40.000000000Z"
    assert agent.handle == "agent"
    assert {entry.color for entry in document.participants} <= set(PARTICIPANT_COLORS)


def test_a_visiting_agent_is_offline_past_the_window_and_a_colliding_name_is_suffixed(tmp_path: Path) -> None:
    source = _source(tmp_path)
    source.share_grants.share_grants_path.write_text('[workspace]\nusers = ["user-agent-0001"]\n')
    source.agent_grants.agent_grants_path.write_text(_AGENT_GRANTS.replace("Josh's Claude", "agent"))
    source.agent_last_seen_path.parent.mkdir(parents=True)
    source.agent_last_seen_path.write_text(
        json.dumps({"va-0123456789abcdef0123456789abcdef": (_T0 - PRESENCE_CONNECTED_WINDOW).isoformat()})
    )
    source.presence.heartbeat(RequestIdentity(owner=False, user_id="user-agent-0001", email="agent@example.com"), _T0)

    document = source.document(_T0)

    person, claude = document.participants[1:3]
    # The workspace agent's handle is reserved first, so a person called "agent" is "agent-2" and the visiting agent
    # named the same is "agent-3".
    assert (person.display_name, person.handle) == ("agent", "agent-2")
    assert (claude.handle, claude.is_connected) == ("agent-3", False)
    assert document.participants[-1].handle == "agent"


def test_the_watch_waits_for_a_files_directory_to_appear_then_announces_and_follows_it(tmp_path: Path) -> None:
    grants_path = tmp_path / "agent_access" / "grants.toml"
    announced: list[int] = []
    watch = RosterWatch(
        watched_paths=(grants_path,), announce=lambda: announced.append(1), retry_interval_seconds=0.01
    )
    watch.start()
    try:
        assert watch.pending_paths == (grants_path,)
        grants_path.parent.mkdir()
        grants_path.write_text("[[agents]]\n")
        wait_for(lambda: watch.pending_paths == (), timeout=5.0, poll_interval=0.01, error_message="never watched")
        # The late watch announces once, since the file was written before it began.
        wait_for(lambda: len(announced) >= 1, timeout=5.0, poll_interval=0.01, error_message="never announced")
        seen = len(announced)
        grants_path.write_text("[[agents]]\n# changed\n")
        wait_for(lambda: len(announced) > seen, timeout=5.0, poll_interval=0.01, error_message="the write was missed")
    finally:
        watch.stop()


def test_a_persons_picture_is_the_shells_own_route_versioned_by_the_connectors_url(tmp_path: Path) -> None:
    """The roster never names the connector's picture URL (the pages admit no outside image): a person with a
    picture is pointed at the shell's picture route, versioned so a newer picture is a newer URL (4.2)."""
    source = _source(tmp_path)
    source.share_grants.share_grants_path.write_text(_GRANTS)
    share_env_path = tmp_path / "share.env"
    share_env_path.write_text("SHARE_BROKER_URL=https://broker.example\n")

    def connector(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/user-bob-4471/profile":
            return httpx.Response(
                200,
                json={"user_id": "user-bob-4471", "display_name": "Bob", "profile_picture_url": "https://a/bob.png"},
            )
        return httpx.Response(404, json={"detail": "no such user"})

    pictured = source.model_copy_update(
        to_update(
            source.field_ref().profiles,
            ProfileResolver(
                cache_directory=tmp_path / "profiles",
                share_env_path=share_env_path,
                transport=httpx.MockTransport(connector),
            ),
        )
    )
    source.presence.heartbeat(_BOB, _T0)

    bob = next(entry for entry in pictured.document(_T0).participants if str(entry.key) == "user:user-bob-4471")
    assert (
        bob.profile_picture_url
        == f"/api/roster/user:user-bob-4471/picture?v={picture_version_of('https://a/bob.png')}"
    )
    assert bob.display_name == "Bob"
