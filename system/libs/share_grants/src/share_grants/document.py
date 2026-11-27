"""The people's grants document as a client reads and replaces it whole: the workspace routes' side of ``grants``.

A client outside the workspace (the Imbue Studio desktop, or the hosted web client) edits who may visit by
sending the whole document, with what it knows of each granted account (the identities sidecar the shell names
a granted account from before it first arrives). This module reads the sharing state a client asks for before
every edit, checks a document's shape, and writes the identities and then the grants, the grants under the
lock every writer of the file holds.

Only the shape is checked: an entry is empty, is repeated, or is not an email address or a domain. Who an
account may share with (its own address, a public email provider's domain, a domain that is not its own) is
the client's to decide and the connector's to enforce; a workspace holds neither the account's address nor a
list of public providers. The value of an entry the stored document already holds is not checked, so a grant
in force is never refused by a later save.

Addresses and domains are compared, and stored, trimmed and lowercased (a domain also loses a leading ``@``),
which is the form the gateway compares a visitor's address against. A refusal names that normalized form in
its message and echoes the raw entry in its ``value``, so a client can mark the row its user typed.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final, NamedTuple

from share_grants.grants import (
    GrantList,
    Grants,
    GrantsError,
    locked_grants_file,
    parse_grants,
    render_grants,
    write_grants_atomic,
    write_text_atomic,
)

_logger = logging.getLogger(__name__)

# Relative to the workspace root every supervised process runs from.
SHARE_ENV_RELATIVE_PATH: Final[Path] = Path("data/.secrets/share.env")
SHARE_GRANTS_RELATIVE_PATH: Final[Path] = Path("data/.secrets/share_grants.toml")
SHARE_IDENTITIES_RELATIVE_PATH: Final[Path] = Path(
    "data/.secrets/share_identities.json"
)
SHARE_IDENTITIES_FILE_VERSION: Final[int] = 1
_IDENTITIES_FILE_MODE: Final[int] = 0o600

# The scope a refusal names for the workspace-level lists; every other scope is named by the app whose list
# carried the entry.
WORKSPACE_GRANT_SCOPE: Final[str] = "workspace"
EMAIL_GRANT_KIND: Final[str] = "email"
EMAIL_DOMAIN_GRANT_KIND: Final[str] = "email_domain"
USER_GRANT_KIND: Final[str] = "user"

# Deliberately simpler than RFC 5321, and the rule the Imbue Studio clients apply too. ``[^\W_]`` is "letter or
# digit" in any script, so an internationalized label passes as written.
_DOMAIN_LABEL: Final[str] = r"[^\W_](?:[^\W_]|-)*(?<!-)"
# Labels carry inner hyphens; the last one is letters only, at least two.
_DOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*\.[^\W\d_]{{2,}}$"
)
_EMAIL_LOCAL_PART_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+$")


class GrantEntries(NamedTuple):
    """One scope's lists as a client sent them: unnormalized, in order, duplicates and blanks included."""

    users: tuple[str, ...]
    emails: tuple[str, ...]
    email_domains: tuple[str, ...]


class GrantsEntries(NamedTuple):
    """A whole grants document as a client sent it."""

    workspace: GrantEntries
    services: Mapping[str, GrantEntries]


class GrantShapeRefusal(NamedTuple):
    """One entry a grants document may not carry, and why."""

    scope: str
    kind: str
    value: str
    message: str


class GrantedIdentityEntry(NamedTuple):
    """What a client knew of one granted account when it saved the grants; None for what it did not know."""

    email: str | None
    display_name: str | None


class SharingState(NamedTuple):
    """What a client reads before it edits the grants."""

    is_shared: bool
    # None when there is no grants file, and when the file holds something that is not a grants document.
    grants: Grants | None
    is_grants_unreadable: bool


def normalize_grant_address(value: str) -> str:
    """The form an address is compared by, and stored as."""
    return value.strip().lower()


def normalize_grant_user_id(value: str) -> str:
    """The form a user id is compared by, and stored as (opaque, so never case-folded)."""
    return value.strip()


def normalize_grant_domain(value: str) -> str:
    """The form a domain is compared by, and stored as (``@example.com`` is the same grant as ``example.com``)."""
    return normalize_grant_address(value).removeprefix("@")


def is_email_domain(value: str) -> bool:
    return _DOMAIN_PATTERN.match(normalize_grant_domain(value)) is not None


def is_email_address(value: str) -> bool:
    local_part, separator, domain = normalize_grant_address(value).partition("@")
    return (
        bool(separator)
        and _EMAIL_LOCAL_PART_PATTERN.match(local_part) is not None
        and is_email_domain(domain)
    )


def _user_refusal_message(
    normalized: str, already_granted: AbstractSet[str], stored: AbstractSet[str]
) -> str | None:
    # Emptiness is judged before duplication: a blank is not an entry at all, so calling a second blank a
    # duplicate of the first would name the wrong thing.
    if normalized in stored:
        return None
    if not normalized:
        return "This entry is empty."
    if normalized in already_granted:
        return f"{normalized} is already granted here."
    return None


def _email_refusal_message(
    normalized: str, already_granted: AbstractSet[str], stored: AbstractSet[str]
) -> str | None:
    if normalized in already_granted:
        return f"{normalized} is already granted here."
    if normalized in stored:
        return None
    if not is_email_address(normalized):
        return f"{normalized} is not an email address."
    return None


def _email_domain_refusal_message(
    normalized: str, already_granted: AbstractSet[str], stored: AbstractSet[str]
) -> str | None:
    if normalized in already_granted:
        return f"{normalized} is already granted here."
    if normalized in stored:
        return None
    if not is_email_domain(normalized):
        return f"{normalized} is not a domain."
    return None


def _refuse_entries(
    entries: GrantEntries, stored: GrantList | None, scope: str
) -> list[GrantShapeRefusal]:
    """The refusals of one scope's entries, each checked against the list it sits in before its own value is."""
    stored_users = stored.users if stored is not None else set()
    stored_emails = stored.emails if stored is not None else set()
    stored_domains = stored.email_domains if stored is not None else set()
    refusals: list[GrantShapeRefusal] = []
    for kind, values, normalize, refusal_message, stored_values in (
        (
            USER_GRANT_KIND,
            entries.users,
            normalize_grant_user_id,
            _user_refusal_message,
            stored_users,
        ),
        (
            EMAIL_GRANT_KIND,
            entries.emails,
            normalize_grant_address,
            _email_refusal_message,
            stored_emails,
        ),
        (
            EMAIL_DOMAIN_GRANT_KIND,
            entries.email_domains,
            normalize_grant_domain,
            _email_domain_refusal_message,
            stored_domains,
        ),
    ):
        already_granted: set[str] = set()
        for value in values:
            normalized = normalize(value)
            message = refusal_message(normalized, already_granted, stored_values)
            if message is not None:
                refusals.append(
                    GrantShapeRefusal(
                        scope=scope, kind=kind, value=value, message=message
                    )
                )
            already_granted.add(normalized)
    return refusals


def grant_shape_refusals(
    document: GrantsEntries, stored: Grants | None
) -> list[GrantShapeRefusal]:
    """Every entry ``document`` adds whose shape is wrong, workspace scope first then each app's."""
    refusals = _refuse_entries(
        document.workspace, stored.workspace if stored else None, WORKSPACE_GRANT_SCOPE
    )
    for service_name in sorted(document.services):
        stored_list = stored.services.get(service_name) if stored is not None else None
        refusals.extend(
            _refuse_entries(document.services[service_name], stored_list, service_name)
        )
    return refusals


def _grant_list(entries: GrantEntries) -> GrantList:
    return GrantList(
        users=list(entries.users),
        emails=list(entries.emails),
        email_domains=list(entries.email_domains),
    )


def normalized_grants(document: GrantsEntries) -> Grants:
    """``document`` in the form it is stored in, so the file holds what was checked."""
    return Grants(
        workspace=_grant_list(document.workspace),
        services={
            name: _grant_list(entries) for name, entries in document.services.items()
        },
    )


def read_sharing_state(workspace_root: Path) -> SharingState:
    """Whether the workspace holds share materials, and its grants.

    A grants file that holds something else than a grants document reads as none, so that a save replaces it;
    one that is there and cannot be read at all is reported, since replacing a policy nobody could read would
    silently drop it.
    """
    is_shared = (workspace_root / SHARE_ENV_RELATIVE_PATH).is_file()
    grants_path = workspace_root / SHARE_GRANTS_RELATIVE_PATH
    try:
        text = grants_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SharingState(
            is_shared=is_shared, grants=None, is_grants_unreadable=False
        )
    except (OSError, UnicodeDecodeError) as exc:
        _logger.warning("Could not read the grants file at %s: %s", grants_path, exc)
        return SharingState(is_shared=is_shared, grants=None, is_grants_unreadable=True)
    try:
        grants: Grants | None = parse_grants(text)
    except GrantsError as exc:
        _logger.warning(
            "The grants file at %s is not a grants document (%s); reading it as none",
            grants_path,
            exc,
        )
        grants = None
    return SharingState(is_shared=is_shared, grants=grants, is_grants_unreadable=False)


def render_share_identities(identities: Mapping[str, GrantedIdentityEntry]) -> str:
    """The identities sidecar as the shell reads it: each granted account's email and display name by id."""
    document = {
        "version": SHARE_IDENTITIES_FILE_VERSION,
        "identities": {
            user_id: {"email": identity.email, "display_name": identity.display_name}
            for user_id, identity in identities.items()
        },
    }
    # sort_keys orders the ids too, so an unchanged set renders byte-identically.
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def replace_grants(
    workspace_root: Path, grants: Grants, identities: Mapping[str, GrantedIdentityEntry]
) -> None:
    """Write the identities and then the grants, the grants under the file's lock and atomically.

    The identities land first: the shell re-reads the roster when the grants change, by which time the names it
    falls back to should be there. The gateway re-reads the grants on every request, so nothing follows the write.
    """
    write_text_atomic(
        workspace_root / SHARE_IDENTITIES_RELATIVE_PATH,
        render_share_identities(identities),
        _IDENTITIES_FILE_MODE,
    )
    grants_path = workspace_root / SHARE_GRANTS_RELATIVE_PATH
    with locked_grants_file(grants_path):
        write_grants_atomic(grants_path, render_grants(grants))


def grant_entries(
    users: Sequence[str], emails: Sequence[str], email_domains: Sequence[str]
) -> GrantEntries:
    return GrantEntries(
        users=tuple(users), emails=tuple(emails), email_domains=tuple(email_domains)
    )


def grants_entries(
    workspace: GrantEntries, entries_by_app: Mapping[str, GrantEntries]
) -> GrantsEntries:
    """A whole document from its workspace-level lists and each app's own."""
    return GrantsEntries(workspace=workspace, services=dict(entries_by_app))


def grant_lists_by_app(grants: Grants) -> dict[str, GrantList]:
    """Each app's own allow-list, in the order of the apps' names."""
    return {name: grants.services[name] for name in sorted(grants.services)}
