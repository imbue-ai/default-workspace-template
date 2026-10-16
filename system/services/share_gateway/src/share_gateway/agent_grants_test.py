from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat

from share_gateway.agent_grants import AUTHORIZED_KEYS_MARKER
from share_gateway.agent_grants import AgentGrant
from share_gateway.agent_grants import AgentGrantsError
from share_gateway.agent_grants import AgentInvite
from share_gateway.agent_grants import AgentKeyError
from share_gateway.agent_grants import AgentScopeError
from share_gateway.agent_grants import WORKSPACE_SCOPE
from share_gateway.agent_grants import canonical_public_key_line
from share_gateway.agent_grants import find_grant_by_token
from share_gateway.agent_grants import find_invite_by_code
from share_gateway.agent_grants import fingerprint_of_public_key_line
from share_gateway.agent_grants import hash_admission_token
from share_gateway.agent_grants import hash_invite_code
from share_gateway.agent_grants import is_agent_id
from share_gateway.agent_grants import mint_admission_token
from share_gateway.agent_grants import mint_agent_id
from share_gateway.agent_grants import mint_invite_code
from share_gateway.agent_grants import normalize_invite_code
from share_gateway.agent_grants import parse_agent_grants
from share_gateway.agent_grants import parse_agent_invites
from share_gateway.agent_grants import reconcile_authorized_keys
from share_gateway.agent_grants import reconcile_authorized_keys_text
from share_gateway.agent_grants import render_agent_grants
from share_gateway.agent_grants import render_agent_invites
from share_gateway.agent_grants import sanitize_agent_name
from share_gateway.agent_grants import validate_scope
from share_gateway.testing import new_ed25519_public_key_line

# The public key and fingerprint of the owner-exec profile's shared test vectors, so the fingerprint rule here is
# checked against the one the daemon uses as keyid.
_VECTOR_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqI4910CfGV/VLbLTy6XXLKZwm/HZQSG/N0iAG0D29c"
_VECTOR_FINGERPRINT = "SHA256:fe85JkIjo8VPe+XqXJGH5Mau1EMFdK1OdKvJUFicyA8"

_NOW = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)


def make_grant(
    agent_id: str = "", scope: "str | list[str]" = WORKSPACE_SCOPE, expires_at: datetime | None = None
) -> AgentGrant:
    public_key, fingerprint = canonical_public_key_line(new_ed25519_public_key_line())
    return AgentGrant(
        agent_id=agent_id or mint_agent_id(),
        name="Josh's Claude",
        public_key=public_key,
        fingerprint=fingerprint,
        token_sha256=hash_admission_token("token-" + fingerprint),
        scope=scope,
        granter="user:8c0f-owner",
        created_at=_NOW,
        expires_at=expires_at,
    )


def test_sanitize_agent_name_strips_controls_quotes_and_whitespace_and_caps_length() -> None:
    assert sanitize_agent_name('  Josh\'s\u200b  "Claude"\tCode\n ') == "Joshs Claude Code"
    assert sanitize_agent_name("‮Imbue Security") == "Imbue Security"
    assert sanitize_agent_name("x" * 100) == "x" * 60
    assert sanitize_agent_name("\u0000‏") == ""


def test_invite_code_shape_and_normalization() -> None:
    code = mint_invite_code()
    assert len(code) == 14 and code.count("-") == 2
    assert all(character in "0123456789ABCDEFGHJKMNPQRSTVWXYZ-" for character in code)
    assert normalize_invite_code(" 7k3m-qxOa 2pfh ") == "7K3MQX0A2PFH"
    assert normalize_invite_code("1l-ii") == "1111"
    assert hash_invite_code(code.lower()) == hash_invite_code(code.replace("-", ""))


def test_admission_token_is_random_and_hashes_stably() -> None:
    first, second = mint_admission_token(), mint_admission_token()
    assert first != second and len(first) >= 40
    assert hash_admission_token(first) == hash_admission_token(first)
    assert hash_admission_token(first) != hash_admission_token(second)


def test_agent_ids_have_the_prefix_and_shape() -> None:
    agent_id = mint_agent_id()
    assert is_agent_id(agent_id)
    assert not is_agent_id("agent-" + "a" * 32)
    assert not is_agent_id("va-short")


def test_canonical_public_key_line_drops_the_comment_and_fingerprints_like_openssh() -> None:
    line, fingerprint = canonical_public_key_line(_VECTOR_PUBLIC_KEY + " agent@laptop")
    assert line == _VECTOR_PUBLIC_KEY
    assert fingerprint == _VECTOR_FINGERPRINT
    assert fingerprint_of_public_key_line(line) == _VECTOR_FINGERPRINT


def test_canonical_public_key_line_refuses_other_key_types_and_garbage() -> None:
    rsa_public = generate_private_key(public_exponent=65537, key_size=2048).public_key()
    rsa_line = rsa_public.public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    with pytest.raises(AgentKeyError):
        canonical_public_key_line(rsa_line)
    with pytest.raises(AgentKeyError):
        canonical_public_key_line("ssh-ed25519 not-base64!!")
    with pytest.raises(AgentKeyError):
        canonical_public_key_line("")


def test_validate_scope_accepts_workspace_and_grantable_apps_only() -> None:
    assert validate_scope("workspace", ["chat"]) == WORKSPACE_SCOPE
    assert validate_scope(["files", "chat", "chat"], ["chat", "files"]) == ["chat", "files"]
    with pytest.raises(AgentScopeError):
        validate_scope(["owner-exec"], ["chat"])
    with pytest.raises(AgentScopeError):
        validate_scope([], ["chat"])
    with pytest.raises(AgentScopeError):
        validate_scope("everything", ["chat"])


def test_grants_render_and_parse_round_trip_including_expiry_and_app_scope() -> None:
    expiring = make_grant(scope=["chat", "files"], expires_at=_NOW + timedelta(days=7))
    grants = [make_grant(), expiring]
    text = render_agent_grants(grants)
    assert "[[agents]]" in text and 'scope = ["chat", "files"]' in text and "expires_at" in text
    assert parse_agent_grants(text) == grants
    assert parse_agent_grants("") == []
    # Control characters a trusted caller may assert, and an emoji a sanitized name keeps, both survive the file.
    odd = make_grant()
    odd.granter = "user:a\rb\x00\x7f"
    odd.name = "Josh's Claude \U0001f600"
    assert parse_agent_grants(render_agent_grants([odd])) == [odd]


def test_parse_agent_grants_fails_closed_on_malformation() -> None:
    with pytest.raises(AgentGrantsError):
        parse_agent_grants("[[agents]]\nagent_id = 'va-nope'\n")
    with pytest.raises(AgentGrantsError):
        parse_agent_grants("agents = 3\n")
    with pytest.raises(AgentGrantsError):
        parse_agent_grants("not toml at all [[")
    duplicated = render_agent_grants([make_grant(agent_id="va-" + "1" * 32), make_grant(agent_id="va-" + "1" * 32)])
    with pytest.raises(AgentGrantsError):
        parse_agent_grants(duplicated)


def test_invites_render_and_parse_round_trip() -> None:
    invite = AgentInvite(
        code_sha256=hash_invite_code("7K3M-QX9A-2PFH"),
        scope=WORKSPACE_SCOPE,
        granter="agent:va-" + "2" * 32,
        name_hint="Ada's Codex",
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=15),
        failed_attempts=2,
        grant_expires_at=_NOW + timedelta(days=1),
    )
    text = render_agent_invites([invite])
    assert parse_agent_invites(text) == [invite]
    assert find_invite_by_code([invite], "7k3mqx9a2pfh") is invite
    assert find_invite_by_code([invite], "7K3M-QX9A-2PFX") is None
    with pytest.raises(AgentGrantsError):
        parse_agent_invites(
            "[[invites]]\ncode_sha256 = 'x'\nscope = 'workspace'\ngranter = 'g'\ncreated_at = 'yesterday'\n"
        )


def test_admits_service_follows_the_scope_and_the_registry() -> None:
    workspace_grant = make_grant()
    assert workspace_grant.admits_service(None, ["chat"])
    assert workspace_grant.admits_service("owner-exec", ["chat"])
    app_grant = make_grant(scope=["chat"])
    assert app_grant.admits_service("chat", ["chat", "files"])
    assert not app_grant.admits_service("files", ["chat", "files"])
    assert not app_grant.admits_service(None, ["chat"])
    # An app that has since become internal is no longer grantable, so it is no longer admitted.
    assert not app_grant.admits_service("chat", ["files"])


def test_find_grant_by_token_ignores_expired_grants() -> None:
    live = make_grant()
    expired = make_grant(expires_at=_NOW - timedelta(seconds=1))
    assert find_grant_by_token([expired, live], "token-" + live.fingerprint, _NOW) is live
    assert find_grant_by_token([expired], "token-" + expired.fingerprint, _NOW) is None
    assert find_grant_by_token([live], "wrong", _NOW) is None


def test_reconcile_authorized_keys_text_keeps_foreign_lines_and_owns_only_marked_ones() -> None:
    owner_line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIqI4910CfGV/VLbLTy6XXLKZwm/HZQSG/N0iAG0D29c owner@laptop"
    stale = f"ssh-ed25519 AAAA {AUTHORIZED_KEYS_MARKER} agent_id=va-{'9' * 32}"
    live = make_grant()
    app_only = make_grant(scope=["chat"])
    expired = make_grant(expires_at=_NOW - timedelta(days=1))
    text = reconcile_authorized_keys_text(f"{owner_line}\n{stale}\n", [live, app_only, expired], _NOW)
    lines = text.splitlines()
    assert lines[0] == owner_line
    assert lines[1] == f"{live.public_key} {AUTHORIZED_KEYS_MARKER} agent_id={live.agent_id}"
    assert len(lines) == 2
    assert reconcile_authorized_keys_text("", [], _NOW) == ""


def test_reconcile_authorized_keys_writes_only_when_changed_and_keeps_modes(tmp_path: Path) -> None:
    keys_path = tmp_path / ".ssh" / "authorized_keys"
    grant = make_grant()
    assert reconcile_authorized_keys(keys_path, [grant], _NOW) is True
    assert reconcile_authorized_keys(keys_path, [grant], _NOW) is False
    assert keys_path.stat().st_mode & 0o777 == 0o600
    assert keys_path.parent.stat().st_mode & 0o777 == 0o700
    assert reconcile_authorized_keys(keys_path, [], _NOW) is True
    assert keys_path.read_text() == ""
