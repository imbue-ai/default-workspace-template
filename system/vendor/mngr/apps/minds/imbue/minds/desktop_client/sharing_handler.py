"""Plugin-side helpers for the desktop-client machine-sharing flow.

Sharing is machine-level in the self-hosted relay design: one share per
workspace host, one grants document covering the workspace plus optional
per-service scopes. The connector owns the share record + relay token
(`mngr imbue_cloud shares ...`); authorization lives in the workspace's own
grants file, which the in-workspace share-gateway re-reads on every request.

Enable = connector ``shares create`` -> inject the grants document + share
materials into the workspace (its share-gateway brings up caddy + frpc) ->
the UI polls readiness by probing the real hostname. Disable = clear the
materials + connector ``shares delete`` (the relay token dies, so the tunnel's
next reconnect is rejected even if the materials linger).
"""

import tomllib
from typing import Any
from typing import Final

import httpx
from loguru import logger

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_materials_injection import ShareInjectionError
from imbue.minds.desktop_client.share_materials_injection import build_share_env_text
from imbue.minds.desktop_client.share_materials_injection import clear_share_materials_from_agent
from imbue.minds.desktop_client.share_materials_injection import has_share_materials_in_agent
from imbue.minds.desktop_client.share_materials_injection import inject_share_grants_into_agent
from imbue.minds.desktop_client.share_materials_injection import inject_share_materials_into_agent
from imbue.minds.desktop_client.share_materials_injection import read_share_grants_from_agent
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.state import get_state
from imbue.mngr.primitives import AgentId

# How long the readiness probe waits on a single fetch of the shared hostname
# before treating the share as not-ready-yet.
SHARE_READINESS_PROBE_TIMEOUT_SECONDS: Final[float] = 4.0

# Signals in the plugin's JSON error body for the two failures a user can
# actually do something about. Matched on the message text because the plugin
# reports both under the same ``ImbueCloudAuthError`` class.
_EXPIRED_SESSION_SIGNALS: Final[tuple[str, ...]] = (
    "Session missing in db or has expired",
    "Refresh rejected by connector",
)
_UNVERIFIED_EMAIL_SIGNAL: Final[str] = "Email not verified"


def describe_connector_failure(exc: Exception) -> str:
    """Turn a connector failure into a sentence the user can act on.

    The plugin's own message is written for whoever is reading the logs
    ("Refresh rejected by connector: Session missing in db or has expired").
    The two failures a user can resolve get a plain sentence instead; anything
    else keeps the plugin's message, which still beats pointing at a log file.
    """
    detail = str(exc)
    if any(signal in detail for signal in _EXPIRED_SESSION_SIGNALS):
        return "Your Imbue Cloud session has expired. You may need to log out and log in again."
    if _UNVERIFIED_EMAIL_SIGNAL in detail:
        return "Imbue Cloud has not verified this account's email address. Verify it, then retry."
    return detail


class SharingError(RuntimeError):
    """Raised on a soft sharing failure; carries a single user-presentable message."""


def resolve_account_email_for_workspace(
    session_store: MultiAccountSessionStore | None,
    agent_id: AgentId,
) -> str:
    """Return the email of the account that owns ``agent_id``.

    Raises :class:`SharingError` if no signed-in account is associated
    with the workspace -- without an account the plugin can't make
    authenticated calls to the connector and there's nothing useful for
    the route to do.
    """
    if session_store is None:
        raise SharingError("Session store unavailable; sign in to enable sharing.")
    account = session_store.get_account_for_workspace(str(agent_id))
    if account is None:
        raise SharingError(
            f"Workspace {agent_id} is not associated with any signed-in account; "
            "associate one from the workspace settings page first."
        )
    return str(account.email)


def resolve_agent_for_host(backend_resolver: BackendResolverInterface, host_id: str) -> AgentId:
    """Resolve a machine's ``host-<hex>`` coordinate to its (primary) agent id.

    Raises :class:`SharingError` when no known workspace lives on that host.
    """
    for agent_id in backend_resolver.list_known_workspace_ids():
        display_info = backend_resolver.get_agent_display_info(agent_id)
        if display_info is not None and str(display_info.host_id) == host_id:
            return agent_id
    raise SharingError(f"No workspace is known for machine '{host_id}'.")


def _grants_have_any_grantee(workspace_grants: dict[str, list[str]], service_grants: dict[str, Any]) -> bool:
    if workspace_grants.get("emails") or workspace_grants.get("email_domains"):
        return True
    for grants in service_grants.values():
        if grants.get("emails") or grants.get("email_domains"):
            return True
    return False


def _broker_base_url() -> str:
    config = get_state().client_env_config
    if config is None:
        raise SharingError("Client environment config is unavailable; cannot determine the accounts broker URL.")
    if config.accounts_base_url is not None:
        return str(config.accounts_base_url).rstrip("/")
    return str(config.connector_url).rstrip("/")


def _connector_base_url() -> str:
    config = get_state().client_env_config
    if config is None:
        raise SharingError("Client environment config is unavailable; cannot determine the connector URL.")
    return str(config.connector_url).rstrip("/")


def enable_sharing(
    host_id: str,
    workspace_grants: dict[str, list[str]],
    service_grants: dict[str, dict[str, list[str]]],
    backend_resolver: BackendResolverInterface,
) -> dict[str, Any]:
    """Enable (or update) sharing for one machine with the given grants document.

    When the machine is already actively shared, only the grants file is
    rewritten (no token rotation, no tunnel restart -- the gateway re-reads
    grants per request). Otherwise the full provisioning flow runs: connector
    share + relay token, then materials injection. Returns the sharing-status
    document (state ``provisioning`` until the readiness probe goes green).
    """
    if not _grants_have_any_grantee(workspace_grants, service_grants):
        raise SharingError("Sharing requires at least one email or email domain to grant access to.")
    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.")
    agent_id = resolve_agent_for_host(backend_resolver, host_id)
    account_email = resolve_account_email_for_workspace(get_state().session_store, agent_id)
    return _enable_sharing_with_cli(host_id, agent_id, workspace_grants, service_grants, cli, account_email)


def _enable_sharing_with_cli(
    host_id: str,
    agent_id: AgentId,
    workspace_grants: dict[str, list[str]],
    service_grants: dict[str, dict[str, list[str]]],
    cli: ImbueCloudCli,
    account_email: str,
) -> dict[str, Any]:
    grants_toml = render_grants_toml(workspace_grants, service_grants)

    try:
        existing = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not read the machine's sharing status: {describe_connector_failure(exc)}") from exc

    if existing is not None and existing.state == "active" and has_share_materials_in_agent(agent_id, cli.mngr_caller):
        # Grants-only update: no token rotation, the gateway picks the new
        # grants up on its next request. Gated on the materials actually being
        # present in the workspace: an earlier enable that failed between the
        # connector-side create and the injection leaves the share "active"
        # with no tunnel, and only the full provisioning path below can repair
        # that (the connector reuses the share row and rotates the token).
        try:
            inject_share_grants_into_agent(agent_id, grants_toml, cli.mngr_caller)
        except ShareInjectionError as exc:
            raise SharingError(str(exc)) from exc
        return _share_status_document(host_id, existing, workspace_grants, service_grants)

    try:
        share = cli.create_share(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not enable sharing: {describe_connector_failure(exc)}") from exc
    if share.relay_token is None or not share.relay_endpoint:
        raise SharingError("Sharing enabled but the connector did not return the relay coordinates.")

    share_env_text = build_share_env_text(
        workspace_domain=share.workspace_domain,
        relay_endpoint=share.relay_endpoint,
        relay_token=share.relay_token.get_secret_value(),
        connector_url=_connector_base_url(),
        broker_url=_broker_base_url(),
    )
    try:
        inject_share_grants_into_agent(agent_id, grants_toml, cli.mngr_caller)
        inject_share_materials_into_agent(agent_id, share_env_text, cli.mngr_caller)
    except ShareInjectionError as exc:
        raise SharingError(str(exc)) from exc
    return _share_status_document(host_id, share, workspace_grants, service_grants)


def _share_status_document(
    host_id: str,
    share: ShareCliInfo,
    workspace_grants: dict[str, list[str]],
    service_grants: dict[str, dict[str, list[str]]],
) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "enabled": share.state == "active",
        "workspace_domain": share.workspace_domain,
        "url": f"https://{share.workspace_domain}/" if share.workspace_domain else None,
        "region": share.region,
        "last_tunnel_login_at": share.last_tunnel_login_at,
        "cert_not_after": share.cert_not_after,
        "grants": {"workspace": workspace_grants, "services": service_grants},
    }


def _parse_grant_list(value: object) -> dict[str, list[str]]:
    """Coerce one grants scope read back from the workspace into ``{emails, email_domains}``."""
    if not isinstance(value, dict):
        return {"emails": [], "email_domains": []}
    emails = value.get("emails", [])
    email_domains = value.get("email_domains", [])
    return {
        "emails": [str(email) for email in emails] if isinstance(emails, list) else [],
        "email_domains": [str(domain) for domain in email_domains] if isinstance(email_domains, list) else [],
    }


def _parse_grants_toml(grants_toml_text: str) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]]]:
    """Parse a grants document read back from the workspace, tolerating malformation as empty."""
    try:
        raw = tomllib.loads(grants_toml_text)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Malformed grants document read back from the workspace: {}", exc)
        return {"emails": [], "email_domains": []}, {}

    workspace_grants = _parse_grant_list(raw.get("workspace"))
    raw_services = raw.get("services", {})
    service_grants = (
        {str(name): _parse_grant_list(value) for name, value in raw_services.items()}
        if isinstance(raw_services, dict)
        else {}
    )
    return workspace_grants, service_grants


def get_sharing(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
) -> dict[str, Any]:
    """Return the machine's sharing document: enabled/domain/status + the grants read from the workspace."""
    empty_grants: dict[str, list[str]] = {"emails": [], "email_domains": []}
    disabled: dict[str, Any] = {
        "host_id": host_id,
        "enabled": False,
        "workspace_domain": None,
        "url": None,
        "region": None,
        "last_tunnel_login_at": None,
        "cert_not_after": None,
        "grants": {"workspace": empty_grants, "services": {}},
    }
    if cli is None:
        return disabled
    try:
        agent_id = resolve_agent_for_host(backend_resolver, host_id)
        account_email = resolve_account_email_for_workspace(session_store, agent_id)
    except SharingError as exc:
        logger.debug("Sharing status: {}", exc)
        return disabled
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        logger.warning("Failed to read share status for {}: {}", host_id, exc)
        return disabled
    if share is None or share.state != "active":
        return disabled

    grants_toml_text = read_share_grants_from_agent(agent_id, cli.mngr_caller)
    workspace_grants, service_grants = _parse_grants_toml(grants_toml_text) if grants_toml_text else (empty_grants, {})
    return _share_status_document(host_id, share, workspace_grants, service_grants)


def disable_sharing(
    host_id: str,
    backend_resolver: BackendResolverInterface,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
) -> None:
    """Disable sharing for a machine: clear the workspace materials, then delete the connector share.

    Idempotent: an already-unshared machine is a success. Raises
    :class:`SharingError` on a missing CLI, no associated account, or a
    connector error.
    """
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured.")
    agent_id = resolve_agent_for_host(backend_resolver, host_id)
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    clear_share_materials_from_agent(agent_id, cli.mngr_caller)
    try:
        existing = cli.get_share_status(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not read the machine's sharing status: {describe_connector_failure(exc)}") from exc
    if existing is None or existing.state != "active":
        return
    try:
        cli.delete_share(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        raise SharingError(f"Could not stop sharing: {describe_connector_failure(exc)}") from exc


def delete_share_for_host(cli: ImbueCloudCli | None, account_email: str, host_id: str) -> None:
    """Delete the account's machine share for ``host_id``, if it has an active one.

    A share left behind keeps a relay hostname reserved and counts against the
    account's shared-machine quota, which would become a ceiling on machines
    ever created rather than on live ones.

    Never raises: this runs on teardown paths (unlinking, destroy
    finalization) where a connector hiccup must not block retiring the
    workspace. A share that survives is litter; a workspace that cannot be
    retired is a stuck UI.
    """
    if cli is None or not host_id.startswith("host-"):
        return
    try:
        share = cli.get_share_status(account=account_email, host_id=host_id)
        if share is not None and share.state == "active":
            cli.delete_share(account=account_email, host_id=host_id)
    except ImbueCloudCliError as exc:
        logger.warning("Failed to delete the machine share for {}: {}", host_id, exc)


def probe_share_readiness(http_client: httpx.Client, workspace_domain: str) -> bool:
    """Report whether the shared hostname is live end to end.

    Reaching the workspace's gateway means DNS, the relay's SNI splice, the
    tunnel, caddy's TLS termination with a real certificate, and the gateway
    itself all work -- any HTTP response (the broker redirect for an
    unauthenticated visit, a 403, anything) counts as ready. Transport errors
    (DNS, TLS, connection) mean not-ready-yet. The domain comes from the
    connector's share record, never from caller input.
    """
    try:
        http_client.get(f"https://{workspace_domain}/", timeout=SHARE_READINESS_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.debug("Probed share domain {} but it is not ready yet: {}", workspace_domain, exc)
        return False
    return True
