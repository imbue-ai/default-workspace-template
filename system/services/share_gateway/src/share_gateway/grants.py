"""Grants evaluation: who may visit which service of this shared workspace.

The grants document is a TOML file the workspace owner edits through the minds
app. Workspace-level grants admit an email (or a whole email domain) to every
service; per-service grants admit it to exactly that service's origin. A
malformed grants file fails closed: nobody is admitted until it parses again.

    [workspace]
    emails = ["bob@example.com"]
    email_domains = ["example.org"]

    [services.web]
    emails = ["carol@example.com"]
    email_domains = []
"""

import tomllib
from pathlib import Path


class GrantsError(ValueError):
    """Raised when the grants file is malformed (evaluation then fails closed)."""


class GrantList:
    """One scope's allow-list: exact emails plus whole email domains."""

    def __init__(self, emails: list[str], email_domains: list[str]) -> None:
        self.emails = {email.strip().lower() for email in emails if email.strip()}
        self.email_domains = {domain.strip().lower().lstrip("@") for domain in email_domains if domain.strip()}

    def allows(self, email: str) -> bool:
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

    def allows(self, email: str, service_name: str | None) -> bool:
        """Whether ``email`` may visit ``service_name`` (None = the workspace shell).

        A workspace-level grant implies every service. A per-service grant
        admits only that service's origin -- the shell and sibling services
        stay forbidden.
        """
        if self.workspace.allows(email):
            return True
        if service_name is None:
            return False
        service_grants = self.services.get(service_name)
        return service_grants is not None and service_grants.allows(email)

    def allows_any(self, email: str) -> bool:
        """Whether ``email`` has any grant at all (used at login callback time)."""
        if self.workspace.allows(email):
            return True
        return any(service_grants.allows(email) for service_grants in self.services.values())


def _parse_grant_list(raw: object, scope: str) -> GrantList:
    if not isinstance(raw, dict):
        raise GrantsError(f"grants scope {scope!r} must be a table")
    emails = raw.get("emails", [])
    email_domains = raw.get("email_domains", [])
    if not isinstance(emails, list) or not all(isinstance(email, str) for email in emails):
        raise GrantsError(f"grants scope {scope!r}: emails must be a list of strings")
    if not isinstance(email_domains, list) or not all(isinstance(domain, str) for domain in email_domains):
        raise GrantsError(f"grants scope {scope!r}: email_domains must be a list of strings")
    return GrantList(emails=emails, email_domains=email_domains)


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
