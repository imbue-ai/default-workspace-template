"""The agent-access operations: mint an invite, join with one, revoke a grant, list, reconcile.

One module behind both the gateway's ``/_agent/*`` routes and the
``agent-access`` CLI the desktop runs over ``mngr exec``, so the two can never
disagree about what a grant is. Every operation takes the whole agent-access
lock, re-reads the files under it, writes them atomically, reconciles the
``authorized_keys`` lines from the grants file (section 5.3 of the spec), and
records an audit event.
"""

import json
import math
import threading
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from share_gateway.agent_audit import write_agent_access_event
from share_gateway.agent_grants import AGENT_GRANTS_FILE
from share_gateway.agent_grants import AGENT_INVITES_FILE
from share_gateway.agent_grants import AgentGrant
from share_gateway.agent_grants import AgentInvite
from share_gateway.agent_grants import AgentKeyError
from share_gateway.agent_grants import INVITE_LIFETIME
from share_gateway.agent_grants import INVITE_MAX_FAILED_ATTEMPTS
from share_gateway.agent_grants import canonical_public_key_line
from share_gateway.agent_grants import find_invite_by_code
from share_gateway.agent_grants import hash_admission_token
from share_gateway.agent_grants import hash_invite_code
from share_gateway.agent_grants import load_agent_grants
from share_gateway.agent_grants import load_agent_invites
from share_gateway.agent_grants import locked_agent_access
from share_gateway.agent_grants import mint_admission_token
from share_gateway.agent_grants import mint_agent_id
from share_gateway.agent_grants import mint_invite_code
from share_gateway.agent_grants import prune_expired
from share_gateway.agent_grants import prune_expired_invites
from share_gateway.agent_grants import reconcile_authorized_keys
from share_gateway.agent_grants import render_agent_grants
from share_gateway.agent_grants import render_agent_invites
from share_gateway.agent_grants import sanitize_agent_name
from share_gateway.agent_grants import write_text_atomic
from share_gateway.json_values import is_json_object
from share_gateway.log import log

DEFAULT_AUTHORIZED_KEYS_PATH = Path.home() / ".ssh" / "authorized_keys"
MAX_GRANT_LIFETIME = timedelta(days=3650)
_LAST_SEEN_THROTTLE_SECONDS = 60.0


class AgentAccessPaths:
    """Where one workspace keeps its agent-access files."""

    def __init__(
        self,
        grants_path: Path,
        invites_path: Path,
        authorized_keys_path: Path,
        events_dir: Path | None,
    ) -> None:
        self.grants_path = grants_path
        self.invites_path = invites_path
        self.authorized_keys_path = authorized_keys_path
        self.events_dir = events_dir


def default_agent_access_paths(events_dir: Path | None) -> AgentAccessPaths:
    return AgentAccessPaths(
        grants_path=AGENT_GRANTS_FILE,
        invites_path=AGENT_INVITES_FILE,
        authorized_keys_path=DEFAULT_AUTHORIZED_KEYS_PATH,
        events_dir=events_dir,
    )


class JoinRefusedError(ValueError):
    """Raised when a join cannot be honoured; ``status`` is the HTTP status the route answers with."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class RevokeRefusedError(ValueError):
    """Raised when a revoke names no grant the caller may revoke."""


class InvalidGrantLifetimeError(ValueError):
    """Raised when an invite's grant lifetime is not a positive number of days."""


def grant_lifetime_from_days(raw: object) -> timedelta | None:
    """None for no expiry, else the lifetime an inviter asked for, capped at ``MAX_GRANT_LIFETIME``.

    The one rule for every surface that mints an invite (the route's JSON body,
    the CLI's ``--expires-in-days``): a positive finite number of days, so a
    grant can never be minted already expired. Raises InvalidGrantLifetimeError.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw <= 0:
        raise InvalidGrantLifetimeError("expires_in_days must be a positive number of days")
    return min(timedelta(days=float(raw)), MAX_GRANT_LIFETIME)


class MintedInvite:
    """A freshly minted code and the record behind it."""

    def __init__(self, code: str, invite: AgentInvite) -> None:
        self.code = code
        self.invite = invite


class JoinedAgent:
    """A grant together with the admission token shown exactly once."""

    def __init__(self, grant: AgentGrant, token: str, is_rejoin: bool) -> None:
        self.grant = grant
        self.token = token
        self.is_rejoin = is_rejoin


def _write_grants(paths: AgentAccessPaths, grants: list[AgentGrant], now: datetime) -> None:
    write_text_atomic(paths.grants_path, render_agent_grants(grants), 0o600)
    reconcile_authorized_keys(paths.authorized_keys_path, grants, now)


def _split_expired(grants: Sequence[AgentGrant], now: datetime) -> tuple[list[AgentGrant], list[AgentGrant]]:
    """``(active, expired)``: every writer drops the expired grants and records each one once it has."""
    return prune_expired(grants, now), [grant for grant in grants if grant.is_expired(now)]


def _record_expired(paths: AgentAccessPaths, expired: Sequence[AgentGrant]) -> None:
    for grant in expired:
        write_agent_access_event(paths.events_dir, "expired", {"agent_id": grant.agent_id, "name": grant.name})


def _write_invites(paths: AgentAccessPaths, invites: list[AgentInvite]) -> None:
    write_text_atomic(paths.invites_path, render_agent_invites(invites), 0o600)


def list_grants(paths: AgentAccessPaths, now: datetime) -> list[AgentGrant]:
    """Every unexpired grant. Raises AgentGrantsError on a malformed file."""
    return prune_expired(load_agent_grants(paths.grants_path), now)


def reconcile(paths: AgentAccessPaths, now: datetime) -> bool:
    """Prune expired grants and invites and bring ``authorized_keys`` in line; True when anything changed."""
    with locked_agent_access(paths.grants_path):
        active_grants, expired_grants = _split_expired(load_agent_grants(paths.grants_path), now)
        is_changed = False
        if expired_grants:
            write_text_atomic(paths.grants_path, render_agent_grants(active_grants), 0o600)
            _record_expired(paths, expired_grants)
            is_changed = True
        if reconcile_authorized_keys(paths.authorized_keys_path, active_grants, now):
            is_changed = True
        invites = load_agent_invites(paths.invites_path)
        active_invites = prune_expired_invites(invites, now)
        if len(active_invites) != len(invites):
            _write_invites(paths, active_invites)
            is_changed = True
    return is_changed


def mint_invite(
    paths: AgentAccessPaths,
    scope: "str | list[str]",
    granter: str,
    name_hint: str,
    grant_lifetime: timedelta | None,
    now: datetime,
    via: str,
) -> MintedInvite:
    """Record a new one-time code (valid for ``INVITE_LIFETIME``) and answer its plaintext once.

    ``granter`` is what the caller asserts; ``via`` says which surface asserted
    it (the gateway, which verified the caller, or the CLI, which trusts whoever
    reached the workspace with exec) and is recorded with the event.
    """
    code = mint_invite_code()
    invite = AgentInvite(
        code_sha256=hash_invite_code(code),
        scope=scope,
        granter=granter,
        name_hint=sanitize_agent_name(name_hint),
        created_at=now,
        expires_at=now + INVITE_LIFETIME,
        failed_attempts=0,
        grant_expires_at=None if grant_lifetime is None else now + grant_lifetime,
    )
    with locked_agent_access(paths.grants_path):
        invites = prune_expired_invites(load_agent_invites(paths.invites_path), now)
        _write_invites(paths, [*invites, invite])
    write_agent_access_event(
        paths.events_dir,
        "invite_minted",
        {
            "granter": granter,
            "via": via,
            "scope": scope,
            "expires_at": invite.expires_at.isoformat(),
        },
    )
    return MintedInvite(code=code, invite=invite)


def _record_failed_attempt(paths: AgentAccessPaths, invites: list[AgentInvite], invite: AgentInvite) -> None:
    invite.failed_attempts += 1
    if invite.failed_attempts >= INVITE_MAX_FAILED_ATTEMPTS:
        remaining = [entry for entry in invites if entry is not invite]
        write_agent_access_event(paths.events_dir, "invite_burned", {"granter": invite.granter})
    else:
        remaining = invites
    _write_invites(paths, remaining)


def join(
    paths: AgentAccessPaths,
    raw_code: str,
    raw_public_key: str,
    raw_name: str,
    now: datetime,
) -> JoinedAgent:
    """Consume ``raw_code`` for the key and name an agent presents; the grant and its token come back.

    A key that already holds a grant re-joins: the agent id stays, the name,
    scope, and granter come from this invite, and a fresh token replaces the
    old one. Raises JoinRefusedError (with the HTTP status) for every refusal.
    """
    name = sanitize_agent_name(raw_name)
    if not name:
        raise JoinRefusedError("name must contain at least one printable character", 400)
    try:
        public_key, fingerprint = canonical_public_key_line(raw_public_key)
    except AgentKeyError as exc:
        raise JoinRefusedError(str(exc), 400) from exc

    with locked_agent_access(paths.grants_path):
        invites = prune_expired_invites(load_agent_invites(paths.invites_path), now)
        invite = find_invite_by_code(invites, raw_code)
        if invite is None:
            # Unknown and expired codes read the same, and a wrong guess against
            # a live code is what burns it; nothing to burn here.
            raise JoinRefusedError("this code is not valid or has expired", 403)
        grants, expired_grants = _split_expired(load_agent_grants(paths.grants_path), now)
        token = mint_admission_token()
        existing = next((grant for grant in grants if grant.fingerprint == fingerprint), None)
        if existing is None:
            grant = AgentGrant(
                agent_id=mint_agent_id(),
                name=name,
                public_key=public_key,
                fingerprint=fingerprint,
                token_sha256=hash_admission_token(token),
                scope=invite.scope,
                granter=invite.granter,
                created_at=now,
                expires_at=invite.grant_expires_at,
            )
            grants.append(grant)
        else:
            grant = AgentGrant(
                agent_id=existing.agent_id,
                name=name,
                public_key=public_key,
                fingerprint=fingerprint,
                token_sha256=hash_admission_token(token),
                scope=invite.scope,
                granter=invite.granter,
                created_at=existing.created_at,
                expires_at=invite.grant_expires_at,
            )
            grants = [grant if entry.agent_id == existing.agent_id else entry for entry in grants]
        _write_grants(paths, grants, now)
        _write_invites(paths, [entry for entry in invites if entry is not invite])
    _record_expired(paths, expired_grants)
    write_agent_access_event(
        paths.events_dir,
        "joined",
        {
            "agent_id": grant.agent_id,
            "name": grant.name,
            "fingerprint": grant.fingerprint,
            "scope": grant.scope,
            "granter": grant.granter,
            "is_rejoin": existing is not None,
        },
    )
    return JoinedAgent(grant=grant, token=token, is_rejoin=existing is not None)


def count_failed_join(paths: AgentAccessPaths, raw_code: str, now: datetime) -> None:
    """Record a failed attempt against a live invite (a wrong key or name after a valid code), burning it at the cap."""
    with locked_agent_access(paths.grants_path):
        invites = prune_expired_invites(load_agent_invites(paths.invites_path), now)
        invite = find_invite_by_code(invites, raw_code)
        if invite is not None:
            _record_failed_attempt(paths, invites, invite)


def revoke(
    paths: AgentAccessPaths,
    agent_id: str,
    revoker: str,
    is_owner: bool,
    now: datetime,
    via: str,
) -> AgentGrant:
    """Remove one grant (and its ``authorized_keys`` line); the owner may revoke any, a granter their own.

    ``revoker`` and ``is_owner`` are what the caller asserts and ``via`` which
    surface asserted them (see ``mint_invite``). Raises RevokeRefusedError when
    no such grant exists or the caller may not revoke it.
    """
    with locked_agent_access(paths.grants_path):
        grants, expired_grants = _split_expired(load_agent_grants(paths.grants_path), now)
        grant = next((entry for entry in grants if entry.agent_id == agent_id), None)
        if grant is None:
            raise RevokeRefusedError(f"no agent {agent_id}")
        if not is_owner and grant.granter != revoker:
            raise RevokeRefusedError(f"agent {agent_id} was not granted by you")
        _write_grants(paths, [entry for entry in grants if entry.agent_id != agent_id], now)
    _record_expired(paths, expired_grants)
    write_agent_access_event(
        paths.events_dir,
        "revoked",
        {
            "agent_id": grant.agent_id,
            "name": grant.name,
            "revoker": revoker,
            "via": via,
        },
    )
    return grant


class LastSeenStore:
    """When each agent last made a request, written at most once a minute per agent."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._written_at_by_agent_id: dict[str, float] = {}
        self._lock = threading.Lock()

    def read_all(self) -> dict[str, str]:
        """The map from agent id to last-seen time; an absent file is empty, an unreadable one is logged and read as empty."""
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log(f"Could not read the last-seen times at {self._path}: {exc}")
            return {}
        if not is_json_object(raw):
            log(f"The last-seen times at {self._path} are not a JSON object; reading them as empty")
            return {}
        return {agent_id: value for agent_id, value in raw.items() if isinstance(value, str)}

    def touch(self, agent_id: str, now: datetime, now_monotonic: float) -> None:
        with self._lock:
            last_written = self._written_at_by_agent_id.get(agent_id)
            if last_written is not None and now_monotonic - last_written < _LAST_SEEN_THROTTLE_SECONDS:
                return
            self._written_at_by_agent_id[agent_id] = now_monotonic
            document = self.read_all()
            document[agent_id] = now.isoformat()
            try:
                write_text_atomic(self._path, json.dumps(document), 0o644)
            except OSError as exc:
                log(f"Could not record last-seen for {agent_id}: {exc}")


def visible_grants(grants: list[AgentGrant], viewer: str, is_owner: bool) -> list[AgentGrant]:
    """The grants a viewer may see: all of them for the owner, the ones they granted for anyone else."""
    if is_owner:
        return grants
    return [grant for grant in grants if grant.granter == viewer]


def describe_grant(grant: AgentGrant, last_seen_at: str | None) -> dict[str, object]:
    """The grant as the routes and the CLI report it (never the token hash or the key material)."""
    return {
        "agent_id": grant.agent_id,
        "name": grant.name,
        "fingerprint": grant.fingerprint,
        "scope": grant.scope,
        "granter": grant.granter,
        "created_at": grant.created_at.isoformat(),
        "expires_at": None if grant.expires_at is None else grant.expires_at.isoformat(),
        "last_seen_at": last_seen_at,
    }
