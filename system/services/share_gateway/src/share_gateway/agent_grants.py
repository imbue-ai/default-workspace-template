"""Agent grants: the records admitting visiting agents, and the invites they join with.

A visiting agent is a coding agent running outside the workspace that holds an
**agent grant**: a record bound to the Ed25519 key the agent generated itself,
scoped to the whole workspace (exec authority through owner-exec, every origin
admitted) or to named apps (those origins only, no exec). Agents are a grant
kind of their own beside the user, email, and domain grants of
``share_grants.toml``; they live in their own file so an older desktop that
rewrites that document wholesale can never touch them.

    data/.secrets/agent_access/grants.toml   -- one [[agents]] table per grant
    data/.secrets/agent_access/invites.toml  -- unconsumed one-time codes

The grants file is the source of truth for the ``authorized_keys`` lines that
give a workspace-scope agent exec authority: ``reconcile_authorized_keys``
adds the line for every unexpired workspace-scope grant and removes every
marked line whose grant is gone, so no other writer of that file can leave an
agent with authority the grants file does not show. Every writer holds the
same ``flock`` on the sibling lock file and replaces files atomically, as the
share grants document does.
"""

import base64
import fcntl
import hashlib
import hmac
import re
import secrets
import tomllib
import unicodedata
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

from share_gateway.grants import toml_string
from share_gateway.grants import write_text_atomic
from share_gateway.json_values import is_json_object

AGENT_ACCESS_DIR = Path("data/.secrets/agent_access")
AGENT_GRANTS_FILE = AGENT_ACCESS_DIR / "grants.toml"
AGENT_INVITES_FILE = AGENT_ACCESS_DIR / "invites.toml"
AGENT_ACCESS_STATE_DIR = Path("data/.state/agent_access")

WORKSPACE_SCOPE = "workspace"
AGENT_ID_PREFIX = "va-"
# The marker comment that makes an ``authorized_keys`` line ours to add and remove.
AUTHORIZED_KEYS_MARKER = "imbue-agent-access"
INVITE_LIFETIME = timedelta(minutes=15)
INVITE_MAX_FAILED_ATTEMPTS = 5
MAX_AGENT_NAME_LENGTH = 60
_INVITE_CODE_LENGTH = 12
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_AGENT_ID_PATTERN = re.compile(r"^va-[0-9a-f]{32}$")
_QUOTE_CHARACTERS = "\"'`\u2018\u2019\u201c\u201d"


class AgentGrantsError(ValueError):
    """Raised when an agent-access file is malformed (admission then fails closed)."""


class AgentKeyError(ValueError):
    """Raised when a public key is not a usable Ed25519 OpenSSH key."""


class AgentScopeError(ValueError):
    """Raised when a scope names something that cannot be granted."""


class AgentGrant:
    """One visiting agent's grant."""

    def __init__(
        self,
        agent_id: str,
        name: str,
        public_key: str,
        fingerprint: str,
        token_sha256: str,
        scope: "str | list[str]",
        granter: str,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> None:
        self.agent_id = agent_id
        self.name = name
        self.public_key = public_key
        self.fingerprint = fingerprint
        self.token_sha256 = token_sha256
        self.scope = scope
        self.granter = granter
        self.created_at = created_at
        self.expires_at = expires_at

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentGrant):
            return NotImplemented
        return vars(self) == vars(other)

    def __repr__(self) -> str:
        return f"AgentGrant({self.agent_id!r}, {self.name!r}, scope={self.scope!r})"

    @property
    def is_workspace_scope(self) -> bool:
        return self.scope == WORKSPACE_SCOPE

    @property
    def app_names(self) -> list[str]:
        return [] if isinstance(self.scope, str) else list(self.scope)

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def admits_service(self, service_name: str | None, grantable_service_names: Sequence[str]) -> bool:
        """Whether this grant admits the agent to ``service_name`` (None = the auth origin, which names no service).

        Workspace scope admits every origin, internal apps included. An app
        scope admits exactly the named apps, and only while each is registered
        and grantable (an app that has since become internal is not admitted);
        the shell is a registered app that is never grantable.
        """
        if self.is_workspace_scope:
            return True
        if service_name is None:
            return False
        return service_name in self.app_names and service_name in grantable_service_names


class AgentInvite:
    """One unconsumed one-time code."""

    def __init__(
        self,
        code_sha256: str,
        scope: "str | list[str]",
        granter: str,
        name_hint: str,
        created_at: datetime,
        expires_at: datetime,
        failed_attempts: int,
        grant_expires_at: datetime | None,
    ) -> None:
        self.code_sha256 = code_sha256
        self.scope = scope
        self.granter = granter
        self.name_hint = name_hint
        self.created_at = created_at
        self.expires_at = expires_at
        self.failed_attempts = failed_attempts
        self.grant_expires_at = grant_expires_at

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AgentInvite):
            return NotImplemented
        return vars(self) == vars(other)

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


# --- names, codes, tokens, keys ---


def sanitize_agent_name(raw_name: str) -> str:
    """The granter-written-name rule: NFKC, no control or format characters, no quotes, collapsed whitespace, capped.

    Returns '' when nothing survives, which callers refuse.
    """
    normalized = unicodedata.normalize("NFKC", raw_name)
    kept = [
        " " if character.isspace() else character
        for character in normalized
        if character.isspace()
        or (not unicodedata.category(character).startswith("C") and character not in _QUOTE_CHARACTERS)
    ]
    collapsed = " ".join("".join(kept).split())
    return collapsed[:MAX_AGENT_NAME_LENGTH].strip()


def mint_invite_code() -> str:
    """A fresh code in three groups of four Crockford base32 characters."""
    raw = "".join(secrets.choice(_CROCKFORD_ALPHABET) for _ in range(_INVITE_CODE_LENGTH))
    return "-".join(raw[index : index + 4] for index in range(0, _INVITE_CODE_LENGTH, 4))


def normalize_invite_code(raw_code: str) -> str:
    """Case-insensitive, hyphen-insensitive, with the Crockford look-alikes folded (I and L to 1, O to 0)."""
    folded = raw_code.strip().upper().replace("-", "").replace(" ", "")
    return folded.replace("I", "1").replace("L", "1").replace("O", "0")


def hash_invite_code(raw_code: str) -> str:
    return hashlib.sha256(normalize_invite_code(raw_code).encode("ascii", errors="replace")).hexdigest()


def mint_admission_token() -> str:
    return secrets.token_urlsafe(32)


def hash_admission_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_agent_id() -> str:
    return AGENT_ID_PREFIX + uuid4().hex


def is_agent_id(value: str) -> bool:
    return bool(_AGENT_ID_PATTERN.match(value))


def canonical_public_key_line(raw_line: str) -> tuple[str, str]:
    """``(key line, fingerprint)`` for an Ed25519 OpenSSH public key line; any comment is dropped.

    Raises AgentKeyError for anything that is not exactly an Ed25519 key.
    """
    parts = raw_line.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise AgentKeyError("public key must be an ssh-ed25519 OpenSSH public key line")
    line = f"{parts[0]} {parts[1]}"
    try:
        key = load_ssh_public_key(line.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise AgentKeyError(f"public key could not be parsed: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AgentKeyError("public key must be an Ed25519 key")
    return line, fingerprint_of_public_key_line(line)


def fingerprint_of_public_key_line(line: str) -> str:
    """The standard OpenSSH SHA256 fingerprint (what owner-exec uses as keyid)."""
    blob = base64.b64decode(line.split()[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def validate_scope(raw_scope: object, grantable_service_names: Sequence[str]) -> "str | list[str]":
    """A scope as a request or an invite names it: ``"workspace"`` or a non-empty list of grantable app names.

    Raises AgentScopeError for anything else, including an internal app, which
    only workspace scope reaches.
    """
    if raw_scope == WORKSPACE_SCOPE:
        return WORKSPACE_SCOPE
    if isinstance(raw_scope, list) and raw_scope and all(isinstance(name, str) and name for name in raw_scope):
        unknown = [name for name in raw_scope if name not in grantable_service_names]
        if unknown:
            raise AgentScopeError(f"not a grantable app: {', '.join(sorted(unknown))}")
        return sorted(set(raw_scope))
    raise AgentScopeError('scope must be "workspace" or a non-empty list of app names')


# --- parsing and rendering ---


def _parse_timestamp(raw: object, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise AgentGrantsError(f"{field_name} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentGrantsError(f"{field_name} is not an ISO 8601 timestamp: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise AgentGrantsError(f"{field_name} must carry a timezone: {raw!r}")
    return parsed.astimezone(timezone.utc)


def _parse_optional_timestamp(raw: object, field_name: str) -> datetime | None:
    if raw is None or raw == "":
        return None
    return _parse_timestamp(raw, field_name)


def _parse_scope(raw: object) -> "str | list[str]":
    if raw == WORKSPACE_SCOPE:
        return WORKSPACE_SCOPE
    if isinstance(raw, list) and raw and all(isinstance(name, str) and name for name in raw):
        return sorted(set(raw))
    raise AgentGrantsError('scope must be "workspace" or a non-empty list of app names')


def _required_string(table: dict[str, object], key: str, context: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise AgentGrantsError(f"{context}: {key} must be a non-empty string")
    return value


def _parse_agent_grant(raw: object) -> AgentGrant:
    if not is_json_object(raw):
        raise AgentGrantsError("every [[agents]] entry must be a table")
    agent_id = _required_string(raw, "agent_id", "agent grant")
    if not is_agent_id(agent_id):
        raise AgentGrantsError(f"agent grant: {agent_id!r} is not an agent id")
    return AgentGrant(
        agent_id=agent_id,
        name=_required_string(raw, "name", f"agent grant {agent_id}"),
        public_key=_required_string(raw, "public_key", f"agent grant {agent_id}"),
        fingerprint=_required_string(raw, "fingerprint", f"agent grant {agent_id}"),
        token_sha256=_required_string(raw, "token_sha256", f"agent grant {agent_id}"),
        scope=_parse_scope(raw.get("scope")),
        granter=_required_string(raw, "granter", f"agent grant {agent_id}"),
        created_at=_parse_timestamp(raw.get("created_at"), f"agent grant {agent_id}: created_at"),
        expires_at=_parse_optional_timestamp(raw.get("expires_at"), f"agent grant {agent_id}: expires_at"),
    )


def parse_agent_grants(text: str) -> list[AgentGrant]:
    """Parse the grants file. Raises AgentGrantsError on any malformation."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AgentGrantsError(f"agent grants file is not valid TOML: {exc}") from exc
    raw_agents = raw.get("agents", [])
    if not isinstance(raw_agents, list):
        raise AgentGrantsError("agent grants file: agents must be an array of tables")
    grants = [_parse_agent_grant(entry) for entry in raw_agents]
    seen_ids: set[str] = set()
    for grant in grants:
        if grant.agent_id in seen_ids:
            raise AgentGrantsError(f"agent grants file: duplicate agent id {grant.agent_id}")
        seen_ids.add(grant.agent_id)
    return grants


def _parse_invite(raw: object) -> AgentInvite:
    if not is_json_object(raw):
        raise AgentGrantsError("every [[invites]] entry must be a table")
    failed_attempts = raw.get("failed_attempts", 0)
    if not isinstance(failed_attempts, int) or isinstance(failed_attempts, bool) or failed_attempts < 0:
        raise AgentGrantsError("invite: failed_attempts must be a non-negative integer")
    name_hint = raw.get("name_hint", "")
    if not isinstance(name_hint, str):
        raise AgentGrantsError("invite: name_hint must be a string")
    return AgentInvite(
        code_sha256=_required_string(raw, "code_sha256", "invite"),
        scope=_parse_scope(raw.get("scope")),
        granter=_required_string(raw, "granter", "invite"),
        name_hint=name_hint,
        created_at=_parse_timestamp(raw.get("created_at"), "invite: created_at"),
        expires_at=_parse_timestamp(raw.get("expires_at"), "invite: expires_at"),
        failed_attempts=failed_attempts,
        grant_expires_at=_parse_optional_timestamp(raw.get("grant_expires_at"), "invite: grant_expires_at"),
    )


def parse_agent_invites(text: str) -> list[AgentInvite]:
    """Parse the invites file. Raises AgentGrantsError on any malformation."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AgentGrantsError(f"agent invites file is not valid TOML: {exc}") from exc
    raw_invites = raw.get("invites", [])
    if not isinstance(raw_invites, list):
        raise AgentGrantsError("agent invites file: invites must be an array of tables")
    return [_parse_invite(entry) for entry in raw_invites]


def _toml_scope(scope: "str | list[str]") -> str:
    if isinstance(scope, str):
        return toml_string(scope)
    return "[" + ", ".join(toml_string(name) for name in scope) + "]"


def _toml_timestamp(value: datetime) -> str:
    return toml_string(value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))


def render_agent_grants(grants: Sequence[AgentGrant]) -> str:
    """Render grants in the shape ``parse_agent_grants`` reads."""
    blocks: list[str] = []
    for grant in grants:
        lines = [
            "[[agents]]",
            f"agent_id = {toml_string(grant.agent_id)}",
            f"name = {toml_string(grant.name)}",
            f"public_key = {toml_string(grant.public_key)}",
            f"fingerprint = {toml_string(grant.fingerprint)}",
            f"token_sha256 = {toml_string(grant.token_sha256)}",
            f"scope = {_toml_scope(grant.scope)}",
            f"granter = {toml_string(grant.granter)}",
            f"created_at = {_toml_timestamp(grant.created_at)}",
        ]
        if grant.expires_at is not None:
            lines.append(f"expires_at = {_toml_timestamp(grant.expires_at)}")
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def render_agent_invites(invites: Sequence[AgentInvite]) -> str:
    """Render invites in the shape ``parse_agent_invites`` reads."""
    blocks: list[str] = []
    for invite in invites:
        lines = [
            "[[invites]]",
            f"code_sha256 = {toml_string(invite.code_sha256)}",
            f"scope = {_toml_scope(invite.scope)}",
            f"granter = {toml_string(invite.granter)}",
            f"name_hint = {toml_string(invite.name_hint)}",
            f"created_at = {_toml_timestamp(invite.created_at)}",
            f"expires_at = {_toml_timestamp(invite.expires_at)}",
            f"failed_attempts = {invite.failed_attempts}",
        ]
        if invite.grant_expires_at is not None:
            lines.append(f"grant_expires_at = {_toml_timestamp(invite.grant_expires_at)}")
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


# --- files ---


def agent_access_lock_path(grants_path: Path) -> Path:
    """The one lock every agent-access writer holds (grants, invites, and the authorized_keys reconcile)."""
    return grants_path.with_name(grants_path.name + ".lock")


@contextmanager
def locked_agent_access(grants_path: Path) -> Generator[None, None, None]:
    lock_path = agent_access_lock_path(grants_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_agent_grants(grants_path: Path) -> list[AgentGrant]:
    """Every grant in the file, expired ones included; an absent file is no grants. Raises AgentGrantsError."""
    if not grants_path.exists():
        return []
    try:
        text = grants_path.read_text()
    except OSError as exc:
        raise AgentGrantsError(f"agent grants file {grants_path} is unreadable: {exc}") from exc
    return parse_agent_grants(text)


def load_active_agent_grants(grants_path: Path, now: datetime) -> list[AgentGrant]:
    return [grant for grant in load_agent_grants(grants_path) if not grant.is_expired(now)]


def load_agent_invites(invites_path: Path) -> list[AgentInvite]:
    if not invites_path.exists():
        return []
    try:
        text = invites_path.read_text()
    except OSError as exc:
        raise AgentGrantsError(f"agent invites file {invites_path} is unreadable: {exc}") from exc
    return parse_agent_invites(text)


def find_grant_by_token(grants: Sequence[AgentGrant], token: str, now: datetime) -> AgentGrant | None:
    """The unexpired grant whose stored hash matches ``token`` (compared in constant time), or None."""
    presented = hash_admission_token(token)
    for grant in grants:
        if hmac.compare_digest(grant.token_sha256, presented) and not grant.is_expired(now):
            return grant
    return None


def find_invite_by_code(invites: Sequence[AgentInvite], raw_code: str) -> AgentInvite | None:
    presented = hash_invite_code(raw_code)
    for invite in invites:
        if hmac.compare_digest(invite.code_sha256, presented):
            return invite
    return None


# --- authorized_keys ---


def _authorized_keys_line(grant: AgentGrant) -> str:
    return f"{grant.public_key} {AUTHORIZED_KEYS_MARKER} agent_id={grant.agent_id}"


def is_agent_access_line(line: str) -> bool:
    return f" {AUTHORIZED_KEYS_MARKER} " in line


def reconcile_authorized_keys_text(existing_text: str, grants: Sequence[AgentGrant], now: datetime) -> str:
    """``existing_text`` with exactly one marked line per unexpired workspace-scope grant and no other marked line.

    Lines without the marker (the owner's keys, the desktop's, the cross-workspace
    SSH grants) are kept exactly as they are, in their order.
    """
    kept = [line for line in existing_text.splitlines() if not is_agent_access_line(line)]
    desired = [
        _authorized_keys_line(grant)
        for grant in sorted(grants, key=lambda entry: entry.agent_id)
        if grant.is_workspace_scope and not grant.is_expired(now)
    ]
    lines = kept + desired
    return ("\n".join(lines) + "\n") if lines else ""


def authorized_keys_lock_path(authorized_keys_path: Path) -> Path:
    """The lock every writer of ``authorized_keys`` holds across its read and write (the desktop's SSH grant too)."""
    return authorized_keys_path.with_name(authorized_keys_path.name + ".lock")


def reconcile_authorized_keys(authorized_keys_path: Path, grants: Sequence[AgentGrant], now: datetime) -> bool:
    """Bring the agent lines of ``authorized_keys_path`` in line with ``grants``; True when the file changed.

    The caller holds the agent-access lock; the read-and-replace also holds the
    ``authorized_keys`` lock, which other writers of the file (the desktop's
    cross-workspace SSH grant) take around their own read-and-write so neither
    overwrites the other's lines. The ``.ssh`` directory is created ``0700`` and
    the file replaced atomically ``0600``, so sshd keeps honouring it.
    """
    authorized_keys_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(authorized_keys_lock_path(authorized_keys_path), "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            existing_text = authorized_keys_path.read_text() if authorized_keys_path.exists() else ""
            desired_text = reconcile_authorized_keys_text(existing_text, grants, now)
            if desired_text == existing_text:
                return False
            write_text_atomic(authorized_keys_path, desired_text, 0o600)
            return True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def prune_expired(grants: Sequence[AgentGrant], now: datetime) -> list[AgentGrant]:
    return [grant for grant in grants if not grant.is_expired(now)]


def prune_expired_invites(invites: Sequence[AgentInvite], now: datetime) -> list[AgentInvite]:
    return [invite for invite in invites if not invite.is_expired(now)]
