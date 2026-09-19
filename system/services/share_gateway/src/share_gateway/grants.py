"""Grants evaluation: who may visit which service of this shared workspace.

The grants document is a TOML file the workspace owner edits through the minds
app. Workspace-level grants admit a user to every service; per-service grants
admit it to exactly that service's origin. A malformed grants file fails
closed: nobody is admitted until it parses again.

    [workspace]
    users = ["3f1c..."]
    emails = ["bob@example.com"]
    email_domains = ["example.org"]

    [services.web]
    users = []
    emails = ["carol@example.com"]
    email_domains = []

A ``users`` entry is an account's user id, the durable identity; it is matched
first. An ``emails`` entry is an invitation: once a visitor with that verified
email is admitted, the gateway rewrites the document to hold their user id
instead (``upgrade_invites``), so a later email change on their account never
revokes what the owner granted. Every writer of the file -- this gateway, the
minds desktop through ``mngr exec`` -- holds the same ``flock`` on the sibling
``.lock`` file around its read-modify-write and replaces the file atomically.
"""

import fcntl
import os
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class GrantsError(ValueError):
    """Raised when the grants file is malformed (evaluation then fails closed)."""


class GrantList:
    """One scope's allow-list: user ids, exact emails, and whole email domains."""

    def __init__(self, users: list[str], emails: list[str], email_domains: list[str]) -> None:
        self.users = {user_id.strip() for user_id in users if user_id.strip()}
        self.emails = {email.strip().lower() for email in emails if email.strip()}
        self.email_domains = {domain.strip().lower().lstrip("@") for domain in email_domains if domain.strip()}

    def allows(self, user_id: str, email: str) -> bool:
        if user_id.strip() in self.users:
            return True
        return self.allows_email(email)

    def allows_email(self, email: str) -> bool:
        normalized = email.strip().lower()
        if normalized in self.emails:
            return True
        _, at, domain = normalized.rpartition("@")
        return bool(at) and domain in self.email_domains


class Grants:
    """The full parsed grants document."""

    def __init__(self, workspace: GrantList, services: dict[str, GrantList]) -> None:
        self.workspace = workspace
        self.services = services

    def allows(self, user_id: str, email: str, service_name: str | None) -> bool:
        """Whether the requester may visit ``service_name`` (None = the workspace shell).

        A workspace-level grant implies every service. A per-service grant
        admits only that service's origin -- the shell and sibling services
        stay forbidden. Within a scope the user id is matched before the
        email, and the email before its domain.
        """
        if self.workspace.allows(user_id, email):
            return True
        if service_name is None:
            return False
        service_grants = self.services.get(service_name)
        return service_grants is not None and service_grants.allows(user_id, email)

    def allows_any(self, user_id: str, email: str) -> bool:
        """Whether the requester has any grant at all (used at login callback time)."""
        if self.workspace.allows(user_id, email):
            return True
        return any(service_grants.allows(user_id, email) for service_grants in self.services.values())

    def has_email_invite(self, email: str) -> bool:
        """Whether ``email`` appears as an exact ``emails`` entry in any scope (a domain match is not an invite)."""
        normalized = email.strip().lower()
        if normalized in self.workspace.emails:
            return True
        return any(normalized in service_grants.emails for service_grants in self.services.values())


def _parse_string_list(raw: object, scope: str, key: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
        raise GrantsError(f"grants scope {scope!r}: {key} must be a list of strings")
    return list(raw)


def _parse_grant_list(raw: object, scope: str) -> GrantList:
    if not isinstance(raw, dict):
        raise GrantsError(f"grants scope {scope!r} must be a table")
    return GrantList(
        users=_parse_string_list(raw.get("users", []), scope, "users"),
        emails=_parse_string_list(raw.get("emails", []), scope, "emails"),
        email_domains=_parse_string_list(raw.get("email_domains", []), scope, "email_domains"),
    )


def parse_grants(text: str) -> Grants:
    """Parse a grants TOML document. Raises GrantsError on any malformation."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GrantsError(f"grants file is not valid TOML: {exc}") from exc
    workspace = _parse_grant_list(raw.get("workspace", {}), "workspace")
    raw_services = raw.get("services", {})
    if not isinstance(raw_services, dict):
        raise GrantsError("grants [services] must be a table of per-service tables")
    services = {name: _parse_grant_list(value, f"services.{name}") for name, value in raw_services.items()}
    return Grants(workspace=workspace, services=services)


def load_grants(path: Path) -> Grants:
    """Load the grants file. Raises GrantsError when missing or malformed (fail closed)."""
    if not path.exists():
        raise GrantsError(f"grants file {path} does not exist")
    try:
        text = path.read_text()
    except OSError as exc:
        raise GrantsError(f"grants file {path} is unreadable: {exc}") from exc
    return parse_grants(text)


def _toml_string_array(values: set[str]) -> str:
    quoted = ", ".join('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' for value in sorted(values))
    return f"[{quoted}]"


def _toml_key(name: str) -> str:
    if name.replace("-", "").replace("_", "").isalnum():
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_grant_list(grant_list: GrantList) -> list[str]:
    return [
        f"users = {_toml_string_array(grant_list.users)}",
        f"emails = {_toml_string_array(grant_list.emails)}",
        f"email_domains = {_toml_string_array(grant_list.email_domains)}",
    ]


def render_grants(grants: Grants) -> str:
    """Render a grants document in the shape ``parse_grants`` reads (the minds desktop writes the same shape)."""
    lines = ["[workspace]", *_render_grant_list(grants.workspace)]
    for service_name in sorted(grants.services):
        lines.extend(["", f"[services.{_toml_key(service_name)}]", *_render_grant_list(grants.services[service_name])])
    return "\n".join(lines) + "\n"


def upgrade_invites(grants: Grants, email: str, user_id: str) -> Grants | None:
    """The document with every exact ``emails`` entry for ``email`` replaced by ``user_id`` in that scope, or None when unchanged."""
    normalized = email.strip().lower()
    is_changed = False
    scopes = [grants.workspace, *grants.services.values()]
    for scope in scopes:
        if normalized in scope.emails:
            scope.emails.discard(normalized)
            scope.users.add(user_id)
            is_changed = True
    return grants if is_changed else None


def grants_lock_path(grants_path: Path) -> Path:
    """The sibling lock file every writer of the grants document holds (``share_grants.toml.lock``)."""
    return grants_path.with_name(grants_path.name + ".lock")


@contextmanager
def locked_grants_file(grants_path: Path) -> Iterator[None]:
    """Hold the exclusive ``flock`` on the grants document's sibling lock file."""
    lock_path = grants_lock_path(grants_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def write_grants_atomic(grants_path: Path, text: str) -> None:
    """Replace the grants document through a same-directory temp file and a rename."""
    temp_path = grants_path.with_name(f".{grants_path.name}.{uuid4().hex}")
    temp_path.write_text(text)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, grants_path)


def upgrade_invites_in_file(grants_path: Path, email: str, user_id: str) -> bool:
    """Rewrite the grants document so ``email``'s invitations become ``user_id`` grants; True when it changed.

    The document is re-read under the lock so a concurrent edit (the minds
    desktop saving a whole document) is never torn or overwritten with a
    stale copy. Raises GrantsError when the file is missing or malformed.
    """
    with locked_grants_file(grants_path):
        upgraded = upgrade_invites(load_grants(grants_path), email, user_id)
        if upgraded is None:
            return False
        write_grants_atomic(grants_path, render_grants(upgraded))
        return True
