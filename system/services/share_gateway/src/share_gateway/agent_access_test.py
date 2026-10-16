import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from share_gateway.agent_access import AgentAccessPaths
from share_gateway.agent_access import JoinRefusedError
from share_gateway.agent_access import LastSeenStore
from share_gateway.agent_access import RevokeRefusedError
from share_gateway.agent_access import count_failed_join
from share_gateway.agent_access import describe_grant
from share_gateway.agent_access import join
from share_gateway.agent_access import list_grants
from share_gateway.agent_access import mint_invite
from share_gateway.agent_access import reconcile
from share_gateway.agent_access import revoke
from share_gateway.agent_access import visible_grants
from share_gateway.agent_grants import AUTHORIZED_KEYS_MARKER
from share_gateway.agent_grants import AgentGrantsError
from share_gateway.agent_grants import INVITE_MAX_FAILED_ATTEMPTS
from share_gateway.agent_grants import find_grant_by_token
from share_gateway.agent_grants import load_agent_grants
from share_gateway.agent_grants import load_agent_invites
from share_gateway.testing import new_ed25519_public_key_line

_NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
_OWNER = "user:8c0f-owner"


def make_paths(tmp_path: Path) -> AgentAccessPaths:
    return AgentAccessPaths(
        grants_path=tmp_path / "secrets" / "grants.toml",
        invites_path=tmp_path / "secrets" / "invites.toml",
        authorized_keys_path=tmp_path / "ssh" / "authorized_keys",
        events_dir=tmp_path / "events",
    )


def read_events(paths: AgentAccessPaths) -> list[dict[str, object]]:
    events_file = (paths.events_dir or Path()) / "events.jsonl"
    if not events_file.exists():
        return []
    return [json.loads(line) for line in events_file.read_text().splitlines() if line]


def test_join_consumes_the_invite_writes_the_grant_the_key_line_and_the_events(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    minted = mint_invite(paths, "workspace", _OWNER, "Josh's Claude", None, _NOW, via="test")
    key_line = new_ed25519_public_key_line() + " agent@laptop"

    joined = join(paths, minted.code, key_line, "  Josh\u200b\tClaude  ", _NOW + timedelta(minutes=1))

    assert joined.is_rejoin is False
    assert joined.grant.name == "Josh Claude"
    assert joined.grant.scope == "workspace"
    assert joined.grant.granter == _OWNER
    assert joined.grant.expires_at is None
    assert load_agent_invites(paths.invites_path) == []
    stored = load_agent_grants(paths.grants_path)
    assert stored == [joined.grant]
    assert find_grant_by_token(stored, joined.token, _NOW) == joined.grant
    keys_text = paths.authorized_keys_path.read_text()
    assert keys_text == f"{joined.grant.public_key} {AUTHORIZED_KEYS_MARKER} agent_id={joined.grant.agent_id}\n"
    assert paths.grants_path.stat().st_mode & 0o777 == 0o600
    assert [event["type"] for event in read_events(paths)] == ["invite_minted", "joined"]
    joined_event = read_events(paths)[1]
    assert joined_event["source"] == "agent_access" and joined_event["agent_id"] == joined.grant.agent_id
    assert "token" not in json.dumps(joined_event) or joined.token not in json.dumps(joined_event)


def test_join_with_an_app_scope_writes_no_key_line_and_carries_the_invite_expiry(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    minted = mint_invite(paths, ["chat"], _OWNER, "", timedelta(days=2), _NOW, via="test")
    joined = join(paths, minted.code, new_ed25519_public_key_line(), "Ada's Codex", _NOW)
    assert joined.grant.scope == ["chat"]
    assert joined.grant.expires_at == _NOW + timedelta(days=2)
    assert not paths.authorized_keys_path.exists() or paths.authorized_keys_path.read_text() == ""


def test_join_refuses_unknown_expired_and_reused_codes_the_same_way(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    minted = mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test")
    with pytest.raises(JoinRefusedError) as unknown:
        join(paths, "AAAA-BBBB-CCCC", new_ed25519_public_key_line(), "x", _NOW)
    with pytest.raises(JoinRefusedError) as expired:
        join(paths, minted.code, new_ed25519_public_key_line(), "x", _NOW + timedelta(minutes=16))
    assert unknown.value.status == 403 and expired.value.status == 403
    assert str(unknown.value) == str(expired.value)
    fresh = mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test")
    join(paths, fresh.code, new_ed25519_public_key_line(), "first", _NOW)
    with pytest.raises(JoinRefusedError):
        join(paths, fresh.code, new_ed25519_public_key_line(), "second", _NOW)


def test_join_refuses_a_bad_key_or_empty_name_before_touching_the_invite(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    minted = mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test")
    with pytest.raises(JoinRefusedError) as bad_key:
        join(paths, minted.code, "ssh-rsa AAAA", "x", _NOW)
    assert bad_key.value.status == 400
    with pytest.raises(JoinRefusedError) as bad_name:
        join(paths, minted.code, new_ed25519_public_key_line(), "​", _NOW)
    assert bad_name.value.status == 400
    assert len(load_agent_invites(paths.invites_path)) == 1


def test_failed_attempts_burn_the_invite_at_the_cap(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    minted = mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test")
    for _ in range(INVITE_MAX_FAILED_ATTEMPTS - 1):
        count_failed_join(paths, minted.code, _NOW)
    assert load_agent_invites(paths.invites_path)[0].failed_attempts == INVITE_MAX_FAILED_ATTEMPTS - 1
    count_failed_join(paths, minted.code, _NOW)
    assert load_agent_invites(paths.invites_path) == []
    assert "invite_burned" in [event["type"] for event in read_events(paths)]


def test_rejoin_with_the_same_key_keeps_the_id_and_rotates_the_token(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    key_line = new_ed25519_public_key_line()
    first = join(
        paths,
        mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test").code,
        key_line,
        "first",
        _NOW,
    )
    second = join(
        paths,
        mint_invite(paths, ["chat"], "agent:va-" + "3" * 32, "", None, _NOW, via="test").code,
        key_line,
        "second",
        _NOW,
    )
    assert second.is_rejoin is True
    assert second.grant.agent_id == first.grant.agent_id
    assert second.grant.name == "second" and second.grant.scope == ["chat"]
    assert second.grant.granter == "agent:va-" + "3" * 32
    stored = load_agent_grants(paths.grants_path)
    assert len(stored) == 1
    assert find_grant_by_token(stored, first.token, _NOW) is None
    assert find_grant_by_token(stored, second.token, _NOW) == second.grant
    # The rejoin narrowed the scope to apps, so the exec line is gone.
    assert paths.authorized_keys_path.read_text() == ""


def test_revoke_is_for_the_owner_or_the_granter_and_removes_the_key_line(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    granted_by_visitor = join(
        paths,
        mint_invite(paths, "workspace", "user:visitor", "", None, _NOW, via="test").code,
        new_ed25519_public_key_line(),
        "a",
        _NOW,
    )
    granted_by_owner = join(
        paths,
        mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test").code,
        new_ed25519_public_key_line(),
        "b",
        _NOW,
    )
    with pytest.raises(RevokeRefusedError):
        revoke(
            paths,
            granted_by_owner.grant.agent_id,
            "user:visitor",
            False,
            _NOW,
            via="test",
        )
    with pytest.raises(RevokeRefusedError):
        revoke(paths, "va-" + "0" * 32, _OWNER, True, _NOW, via="test")
    revoke(
        paths,
        granted_by_visitor.grant.agent_id,
        "user:visitor",
        False,
        _NOW,
        via="test",
    )
    revoke(
        paths,
        granted_by_owner.grant.agent_id,
        "user:someone-else",
        True,
        _NOW,
        via="test",
    )
    assert load_agent_grants(paths.grants_path) == []
    assert paths.authorized_keys_path.read_text() == ""
    assert [event["type"] for event in read_events(paths)].count("revoked") == 2


def test_reconcile_prunes_expired_grants_and_restores_the_key_lines(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    kept = join(
        paths,
        mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test").code,
        new_ed25519_public_key_line(),
        "kept",
        _NOW,
    )
    join(
        paths,
        mint_invite(paths, "workspace", _OWNER, "", timedelta(hours=1), _NOW, via="test").code,
        new_ed25519_public_key_line(),
        "gone",
        _NOW,
    )
    paths.authorized_keys_path.write_text("ssh-ed25519 AAAA owner@laptop\n")
    assert reconcile(paths, _NOW + timedelta(hours=2)) is True
    assert [grant.agent_id for grant in load_agent_grants(paths.grants_path)] == [kept.grant.agent_id]
    lines = paths.authorized_keys_path.read_text().splitlines()
    assert lines[0] == "ssh-ed25519 AAAA owner@laptop"
    assert lines[1].endswith(f"agent_id={kept.grant.agent_id}")
    assert reconcile(paths, _NOW + timedelta(hours=2)) is False
    assert "expired" in [event["type"] for event in read_events(paths)]


def test_join_and_revoke_record_the_expired_grants_they_prune(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    for name in ("first-to-expire", "second-to-expire"):
        join(
            paths,
            mint_invite(paths, "workspace", _OWNER, "", timedelta(hours=1), _NOW, via="test").code,
            new_ed25519_public_key_line(),
            name,
            _NOW,
        )
    later = _NOW + timedelta(hours=2)
    kept = join(
        paths,
        mint_invite(paths, "workspace", _OWNER, "", None, later, via="test").code,
        new_ed25519_public_key_line(),
        "kept",
        later,
    )
    assert [grant.agent_id for grant in load_agent_grants(paths.grants_path)] == [kept.grant.agent_id]
    expired_names = [str(event["name"]) for event in read_events(paths) if event["type"] == "expired"]
    assert sorted(expired_names) == ["first-to-expire", "second-to-expire"]

    join(
        paths,
        mint_invite(paths, "workspace", _OWNER, "", timedelta(hours=1), later, via="test").code,
        new_ed25519_public_key_line(),
        "third-to-expire",
        later,
    )
    even_later = later + timedelta(hours=2)
    with pytest.raises(RevokeRefusedError):
        revoke(paths, "va-" + "0" * 32, _OWNER, True, even_later, via="test")
    assert "third-to-expire" not in [event["name"] for event in read_events(paths) if event["type"] == "expired"]
    revoke(paths, kept.grant.agent_id, _OWNER, True, even_later, via="test")
    assert load_agent_grants(paths.grants_path) == []
    assert "third-to-expire" in [event["name"] for event in read_events(paths) if event["type"] == "expired"]


def test_list_and_visibility_and_description(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    mine = join(
        paths,
        mint_invite(paths, "workspace", "user:visitor", "", None, _NOW, via="test").code,
        new_ed25519_public_key_line(),
        "mine",
        _NOW,
    )
    theirs = join(
        paths,
        mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test").code,
        new_ed25519_public_key_line(),
        "theirs",
        _NOW,
    )
    grants = list_grants(paths, _NOW)
    assert visible_grants(grants, "user:visitor", False) == [mine.grant]
    assert visible_grants(grants, "user:nobody", True) == [mine.grant, theirs.grant]
    described = describe_grant(mine.grant, "2026-10-15T19:00:00+00:00")
    assert described["agent_id"] == mine.grant.agent_id and described["last_seen_at"] == "2026-10-15T19:00:00+00:00"
    assert "token" not in described and "public_key" not in described


def test_last_seen_store_reads_an_absent_or_unreadable_file_as_empty_and_rewrites_it(tmp_path: Path) -> None:
    path = tmp_path / "state" / "last_seen.json"
    store = LastSeenStore(path)
    assert store.read_all() == {}
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert store.read_all() == {}
    path.write_text(json.dumps(["a list"]))
    assert store.read_all() == {}
    store.touch("va-" + "1" * 32, _NOW, 0.0)
    assert store.read_all() == {"va-" + "1" * 32: _NOW.isoformat()}


def test_malformed_grants_file_fails_every_operation_closed(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    paths.grants_path.parent.mkdir(parents=True)
    paths.grants_path.write_text("[[agents]]\nagent_id = 'va-bad'\n")
    with pytest.raises(AgentGrantsError):
        list_grants(paths, _NOW)
    minted = mint_invite(paths, "workspace", _OWNER, "", None, _NOW, via="test")
    with pytest.raises(AgentGrantsError):
        join(paths, minted.code, new_ed25519_public_key_line(), "x", _NOW)
