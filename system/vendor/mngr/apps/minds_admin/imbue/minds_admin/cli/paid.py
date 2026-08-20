"""``minds-admin paid ...`` -- operator-only paid-list management.

Manages the connector's ``paid_domains`` / ``paid_emails`` tables via the
admin CRUD API. Authenticated by the fixed ``MINDS_ADMIN_KEY`` API key
(NOT a SuperTokens session); domains and emails are managed separately.

A user counts as "paid" when their verified email is matched by an active
(``is_paid = true``) row in either table: an exact full-email match in the
emails list, or an exact domain match (the part after ``@``) in the domains
list. "remove" is a soft delete (sets ``is_paid = false``); "list" shows all
rows with their status unless ``--paid-only`` is passed.
"""

from typing import Callable

import click
from pydantic import SecretStr

from imbue.minds_admin.cli._tier_secrets import make_admin_connector_client
from imbue.minds_admin.cli._tier_secrets import resolve_admin_api_key_value
from imbue.mngr_imbue_cloud.cli._common import emit_json
from imbue.mngr_imbue_cloud.cli._common import handle_imbue_cloud_errors
from imbue.mngr_imbue_cloud.wire_types import PaidListEntry


def resolve_admin_api_key(explicit: str | None) -> SecretStr:
    """Resolve the admin API key: flag > $MINDS_ADMIN_KEY (or deprecated spelling) > the activated tier's Vault entry."""
    return SecretStr(resolve_admin_api_key_value(explicit))


def paid_auth_options(func: Callable[..., None]) -> Callable[..., None]:
    """Attach the shared ``--connector-url`` / ``--api-key`` options to a command."""
    func = click.option(
        "--connector-url",
        default=None,
        help=(
            "Connector base URL. Defaults to $MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL, "
            "else the activated env's client.toml."
        ),
    )(func)
    func = click.option(
        "--api-key",
        default=None,
        help="Admin API key. Defaults to $MINDS_ADMIN_KEY, else the activated tier's supertokens Vault entry.",
    )(func)
    return func


def _emit_entries(entries: list[PaidListEntry]) -> None:
    emit_json([entry.model_dump() for entry in entries])


@click.group(name="paid")
def paid() -> None:
    """Manage paid domains / emails (requires the MINDS_ADMIN_KEY API key)."""


@paid.group(name="domain")
def domain() -> None:
    """Add / remove / list paid domains (e.g. ``imbue.com``)."""


@paid.group(name="email")
def email() -> None:
    """Add / remove / list paid individual email addresses."""


@domain.command(name="add")
@click.argument("value")
@paid_auth_options
@handle_imbue_cloud_errors
def domain_add(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Add (or reactivate) a paid domain."""
    client = make_admin_connector_client(connector_url)
    emit_json(client.add_paid_domain(resolve_admin_api_key(api_key), value))


@domain.command(name="remove")
@click.argument("value")
@paid_auth_options
@handle_imbue_cloud_errors
def domain_remove(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Soft-remove a paid domain (sets is_paid=false)."""
    client = make_admin_connector_client(connector_url)
    emit_json(client.remove_paid_domain(resolve_admin_api_key(api_key), value))


@domain.command(name="list")
@click.option("--paid-only", is_flag=True, default=False, help="Only show currently-active (is_paid) domains.")
@paid_auth_options
@handle_imbue_cloud_errors
def domain_list(paid_only: bool, connector_url: str | None, api_key: str | None) -> None:
    """List paid domains."""
    client = make_admin_connector_client(connector_url)
    _emit_entries(client.list_paid_domains(resolve_admin_api_key(api_key), paid_only))


@email.command(name="add")
@click.argument("value")
@paid_auth_options
@handle_imbue_cloud_errors
def email_add(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Add (or reactivate) a paid email."""
    client = make_admin_connector_client(connector_url)
    emit_json(client.add_paid_email(resolve_admin_api_key(api_key), value))


@email.command(name="remove")
@click.argument("value")
@paid_auth_options
@handle_imbue_cloud_errors
def email_remove(value: str, connector_url: str | None, api_key: str | None) -> None:
    """Soft-remove a paid email (sets is_paid=false)."""
    client = make_admin_connector_client(connector_url)
    emit_json(client.remove_paid_email(resolve_admin_api_key(api_key), value))


@email.command(name="list")
@click.option("--paid-only", is_flag=True, default=False, help="Only show currently-active (is_paid) emails.")
@paid_auth_options
@handle_imbue_cloud_errors
def email_list(paid_only: bool, connector_url: str | None, api_key: str | None) -> None:
    """List paid emails."""
    client = make_admin_connector_client(connector_url)
    _emit_entries(client.list_paid_emails(resolve_admin_api_key(api_key), paid_only))
