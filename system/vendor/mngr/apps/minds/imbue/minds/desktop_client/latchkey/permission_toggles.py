"""Per-workspace toggle view and editing of latchkey permissions.

Backs the workspace options panel's "Permissions" tab: one screen, scoped to a
single workspace's host, where every grantable permission renders as a toggle.
Flipping a toggle recomputes the *complete* permission set for the affected
rule from the new toggle states and writes it back through the gateway's
``permissions`` extension (the single owner of on-disk permission writes) --
the endpoint always receives the full set, never a diff.

Three families of toggles, mirroring the permission-request types:

* **Connector toggles** -- catalog-backed third-party service permissions,
  granted per ``(scope, account)`` rule (see
  :mod:`imbue.mngr_latchkey.account_scopes`). Toggling writes the rule with
  :func:`build_account_grant`, so the generated per-account schema always
  travels with it; turning the last permission off deletes the rule.
* **File-sharing toggles** -- the ``minds-file-server-*`` permission names on
  the shared ``latchkey-self`` rule. The rule also carries baseline / accounts
  / workspace permissions, so a rewrite always preserves every name this
  module does not own.
* **Machine (cross-workspace) toggles** -- the ``minds-workspaces-*`` verb
  names on the same ``latchkey-self`` rule, preserved the same way.

Enabling a ``latchkey-self`` toggle needs the permission's schema definition to
already exist in the host file (per-path / per-verb schemas are minted by the
gateway when a request is approved; revoking leaves them behind precisely so
the permission can be re-enabled here). A name whose schema is gone cannot be
re-enabled -- detent fails the entire check when a referenced schema is unknown
-- so those rows surface as non-enableable and the agent must re-request.

The desktop client is the only writer: page JS posts one toggle flip, the
route recomputes the full set server-side (so a buggy client can never clobber
unrelated ``latchkey-self`` baselines), and the gateway merges the write.
"""

from collections.abc import Sequence
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.permission_overview import FILE_SHARING_READ_LABEL
from imbue.minds.desktop_client.latchkey.permission_overview import FILE_SHARING_WRITE_LABEL
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.permission_overview import account_label
from imbue.minds.desktop_client.latchkey.permission_overview import parse_file_sharing_permission
from imbue.minds.desktop_client.latchkey.permission_overview import parse_workspace_permission
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_target_workspace_name
from imbue.minds.desktop_client.latchkey.permission_overview import resolve_workspace_host_id
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.account_scopes import list_account_grants
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

# Group used for the ``<service>-read-all`` / ``<service>-write-all`` pair (and
# bare ``read`` / ``write`` scopes like github-git), always rendered first.
_FULL_ACCESS_HEADING: Final[str] = "Full access"
# Group used for the injected detent catch-all ``any``, always rendered last.
_EXTRAS_HEADING: Final[str] = "Extras"

# Leading verb tokens recognized in verb-first permission names
# (``github-read-repos``, ``google-gmail-send-messages``,
# ``notion-mcp-create-pages``). A name starting with one of these reads as
# "<Verb> <object>" and groups by the object.
_LEADING_VERBS: Final[frozenset[str]] = frozenset(
    {
        "read",
        "write",
        "send",
        "get",
        "list",
        "search",
        "create",
        "update",
        "delete",
        "manage",
        "query",
        "fetch",
        "move",
        "duplicate",
        "check",
    }
)


class PermissionToggleError(Exception):
    """Raised for caller-facing problems (unknown scope/permission, unresolvable workspace)."""


class PermissionToggle(FrozenModel):
    """One toggleable catalog permission and its current state on the host."""

    permission: str = Field(description="Detent permission schema name (the stored / wire value).")
    label: str = Field(description="User-facing row label derived from the permission name.")
    description: str = Field(
        default="",
        description="Plain-English summary from the catalog; empty when the catalog has none.",
    )
    is_granted: bool = Field(description="Whether the host's rule currently grants this permission.")


class PermissionToggleGroup(FrozenModel):
    """A titled group of toggles (``Full access``, ``Messages``, ...), mirroring the design's sections."""

    heading: str = Field(description="Group heading rendered as a section eyebrow.")
    toggles: tuple[PermissionToggle, ...] = Field(description="Toggle rows in catalog order.")


class ScopeTogglePanel(FrozenModel):
    """The grouped toggles of one detent scope a service exposes."""

    scope: str = Field(description="Detent scope schema name; the rule key half of the grant.")
    heading: str = Field(description="Scope display name, shown as a divider when a service has several scopes.")
    groups: tuple[PermissionToggleGroup, ...] = Field(description="Toggle groups, full access first, extras last.")


class ConnectionPanel(FrozenModel):
    """One left-nav connection entry: a (service, account) pair with its toggle panels."""

    service_name: str = Field(description="Raw catalog service name (e.g. ``slack``); the revoke-all key.")
    display_name: str = Field(description="Human-readable service label.")
    account: str = Field(
        description='Latchkey account key (``""`` for the unnamed default); the rule-key half of grants.',
    )
    account_label: str = Field(description="User-facing account label (the default account reads as such).")
    is_connected: bool = Field(
        description=(
            "Whether latchkey stores credentials for the account. ``False`` for an account that only "
            "appears in the host's rules; its grants still render so they can be toggled off."
        ),
    )
    show_account_label: bool = Field(
        description="Whether the nav entry needs the account label to disambiguate (service has several accounts).",
    )
    granted_count: int = Field(description="Total permissions currently granted across the service's scopes.")
    scopes: tuple[ScopeTogglePanel, ...] = Field(description="One toggle panel per scope the service exposes.")


class AvailableConnection(FrozenModel):
    """A catalog service with no signed-in account yet, offered by ``Add connection``."""

    service_name: str = Field(description="Raw catalog service name; posted to the add-account route.")
    display_name: str = Field(description="Human-readable service label.")


class SelfPermissionToggle(FrozenModel):
    """One ``latchkey-self`` toggle row (a shared path or a cross-workspace verb)."""

    permission: str = Field(description="Full detent permission name on the ``latchkey-self`` rule.")
    label: str = Field(description="Primary row label (the shared path, or the verb display name).")
    detail: str = Field(
        default="",
        description="Secondary label (access level for paths; target workspace for targeted verbs).",
    )
    description: str = Field(default="", description="Plain-English summary shown under the label.")
    is_granted: bool = Field(description="Whether the ``latchkey-self`` rule currently carries the name.")
    can_enable: bool = Field(
        description=(
            "Whether the toggle can be turned on: the permission's schema definition still exists in "
            "the host file. When ``False`` the agent must request the grant again."
        ),
    )


class WorkspacePermissionsView(FrozenModel):
    """Everything the Permissions tab renders for one workspace."""

    host_id: str = Field(description="Host whose permissions file the toggles edit.")
    connections: tuple[ConnectionPanel, ...] = Field(description="Connected / granted (service, account) pairs.")
    available_connections: tuple[AvailableConnection, ...] = Field(
        description="Catalog services with no account yet, offered by Add connection.",
    )
    file_sharing_toggles: tuple[SelfPermissionToggle, ...] = Field(description="Shared-path toggle rows.")
    workspace_toggles: tuple[SelfPermissionToggle, ...] = Field(description="Cross-workspace verb toggle rows.")


@pure
def _humanize_tokens(tokens: Sequence[str]) -> str:
    """Join name tokens into a lowercase phrase (``("chat", "read")`` is handled by the caller)."""
    return " ".join(tokens)


@pure
def _strip_service_prefix(permission: str, prefixes: Sequence[str]) -> str:
    """Strip the longest matching ``<prefix>-`` from a permission name (or return it unchanged)."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        if permission.startswith(f"{prefix}-"):
            return permission[len(prefix) + 1 :]
    return permission


@pure
def classify_permission(permission: str, scope: str, service_name: str) -> tuple[str, str, str]:
    """Derive ``(group_key, group_heading, label)`` for a catalog permission name.

    Permission names across the catalog follow a handful of conventions --
    ``slack-chat-read`` (verb-last), ``github-read-repos`` (verb-first),
    ``<service>-read-all`` / ``-write-all`` (whole-scope), bare ``aws-s3``
    (no verb) -- so the mapping is heuristic: names are grouped by the object
    they act on and labelled "<Verb> <object>". Unrecognized shapes fall back
    to a group of their own, so nothing is ever dropped. The detent catch-all
    ``any`` always lands in the trailing Extras group.
    """
    if permission == WILDCARD_PERMISSION_NAME:
        return "zz-extras", _EXTRAS_HEADING, "Everything (unrestricted)"
    remainder = _strip_service_prefix(permission, (scope, service_name))
    if remainder in ("read-all", "read"):
        return "aa-full-access", _FULL_ACCESS_HEADING, "Read everything"
    if remainder in ("write-all", "write"):
        return "aa-full-access", _FULL_ACCESS_HEADING, "Change everything"
    if remainder in ("everything", "all"):
        return "aa-full-access", _FULL_ACCESS_HEADING, "Everything"
    tokens = [token for token in remainder.split("-") if token]
    if not tokens:
        return remainder, remainder, permission
    if len(tokens) > 1 and tokens[-1] == "read":
        subject = _humanize_tokens(tokens[:-1])
        return subject, subject.capitalize(), f"Read {subject}"
    if len(tokens) > 1 and tokens[-1] == "write":
        subject = _humanize_tokens(tokens[:-1])
        return subject, subject.capitalize(), f"Manage {subject}"
    if len(tokens) > 1 and tokens[0] in _LEADING_VERBS:
        subject = _humanize_tokens(tokens[1:])
        return subject, subject.capitalize(), f"{tokens[0].capitalize()} {subject}"
    subject = _humanize_tokens(tokens)
    return subject, subject.capitalize(), subject.capitalize()


def _build_scope_panel(info: ServicePermissionInfo, granted: frozenset[str]) -> ScopeTogglePanel:
    """Group one scope's grantable permissions into titled toggle groups.

    Iterates the catalog's declared order (``any`` is index 0 but always
    renders in the trailing Extras group); group order is full access first,
    then first appearance, extras last.
    """
    toggles_by_group: dict[str, tuple[str, list[PermissionToggle]]] = {}
    for permission in info.permission_schemas:
        group_key, heading, label = classify_permission(permission, info.scope, info.name)
        toggle = PermissionToggle(
            permission=permission,
            label=label,
            description=info.description_by_permission_name.get(permission, ""),
            is_granted=permission in granted,
        )
        _, toggles = toggles_by_group.setdefault(group_key, (heading, []))
        toggles.append(toggle)
    ordered_keys = sorted(
        toggles_by_group,
        key=lambda key: (
            key != "aa-full-access",
            key == "zz-extras",
            tuple(toggles_by_group).index(key),
        ),
    )
    groups = tuple(
        PermissionToggleGroup(heading=toggles_by_group[key][0], toggles=tuple(toggles_by_group[key][1]))
        for key in ordered_keys
    )
    return ScopeTogglePanel(scope=info.scope, heading=info.display_name, groups=groups)


def _granted_by_scope_account(
    services_catalog: ServicesCatalog,
    config: LatchkeyPermissionsConfig,
) -> dict[tuple[str, str], frozenset[str]]:
    """Map every catalog-service grant in the file to ``(scope, account) -> permissions``."""
    granted: dict[tuple[str, str], frozenset[str]] = {}
    for grant in services_catalog.list_service_account_grants(config):
        key = (grant.scope, grant.account)
        granted[key] = granted.get(key, frozenset()) | frozenset(grant.permissions)
    return granted


def _sorted_panel_accounts(stored: Sequence[str], granted: frozenset[str]) -> tuple[str, ...]:
    """Order a service's accounts for the nav: stored ones first (as reported), granted-only after."""
    extra = sorted(
        (account for account in granted if account not in stored),
        key=lambda account: (account == DEFAULT_ACCOUNT, account.lower()),
    )
    return tuple(stored) + tuple(extra)


def build_workspace_permissions_view(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    workspace_agent_id: str,
) -> WorkspacePermissionsView:
    """Assemble the Permissions tab's full view for one workspace.

    Reads the workspace host's permissions file once (through the gateway
    extension) and one ``latchkey auth list --offline`` snapshot. A connection
    panel exists for every (service, account) pair that either has stored
    credentials or still appears in the host's rules, so no grant is invisible;
    services with no account at all are offered under Add connection.

    Raises :class:`PermissionToggleError` for an unresolvable workspace and
    lets :class:`LatchkeyGatewayClientError` propagate when the gateway cannot
    be reached (the route renders the unavailable state for both).
    """
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot load permissions.",
        )
    config = gateway_client.get_permissions_config(permissions_path_for_host(latchkey.plugin_data_dir, host_id))
    granted = _granted_by_scope_account(services_catalog, config)
    accounts_by_service = latchkey.auth_list(is_offline=True)

    connections: list[ConnectionPanel] = []
    available: list[AvailableConnection] = []
    for service_name, infos in services_catalog.as_mapping().items():
        if not infos:
            continue
        display_name = infos[0].display_name
        stored = tuple(entry.account for entry in accounts_by_service.get(service_name, ()))
        granted_accounts = frozenset(
            account for (scope, account) in granted if any(info.scope == scope for info in infos)
        )
        panel_accounts = _sorted_panel_accounts(stored, granted_accounts)
        if not panel_accounts:
            available.append(AvailableConnection(service_name=service_name, display_name=display_name))
            continue
        for account in panel_accounts:
            scopes = tuple(_build_scope_panel(info, granted.get((info.scope, account), frozenset())) for info in infos)
            connections.append(
                ConnectionPanel(
                    service_name=service_name,
                    display_name=display_name,
                    account=account,
                    account_label=account_label(account),
                    is_connected=account in stored,
                    show_account_label=len(panel_accounts) > 1,
                    granted_count=sum(len(granted.get((info.scope, account), frozenset())) for info in infos),
                    scopes=scopes,
                )
            )

    return WorkspacePermissionsView(
        host_id=str(host_id),
        connections=tuple(sorted(connections, key=lambda panel: (panel.display_name.lower(), panel.account_label))),
        available_connections=tuple(sorted(available, key=lambda entry: entry.display_name.lower())),
        file_sharing_toggles=build_file_sharing_toggles(config),
        workspace_toggles=build_workspace_toggles(backend_resolver, config),
    )


@pure
def _self_rule_permissions(config: LatchkeyPermissionsConfig) -> tuple[str, ...]:
    """Every permission name on the ``latchkey-self`` rule, in file order (duplicate keys unioned)."""
    merged: list[str] = []
    for rule in config.rules:
        for permission in rule.get(SELF_SCOPE, []):
            if permission not in merged:
                merged.append(permission)
    return tuple(merged)


@pure
def build_file_sharing_toggles(config: LatchkeyPermissionsConfig) -> tuple[SelfPermissionToggle, ...]:
    """Build the Local files toggle rows: granted paths plus revoked-but-restorable ones.

    Candidates are the union of the ``minds-file-server-*`` names on the
    ``latchkey-self`` rule (granted) and the same-shaped names in the file's
    ``schemas`` object -- revocation leaves the per-path schema behind, which is
    exactly what makes an off toggle re-enableable. Sorted by path, read before
    write.
    """
    granted = frozenset(_self_rule_permissions(config))
    candidates = {name for name in granted if parse_file_sharing_permission(name) is not None}
    candidates.update(name for name in config.schemas if parse_file_sharing_permission(name) is not None)
    rows: list[tuple[str, str, SelfPermissionToggle]] = []
    for name in candidates:
        parsed = parse_file_sharing_permission(name)
        if parsed is None:
            continue
        access, path = parsed
        rows.append(
            (
                path,
                access,
                SelfPermissionToggle(
                    permission=name,
                    label=path,
                    detail=FILE_SHARING_WRITE_LABEL if access == "write" else FILE_SHARING_READ_LABEL,
                    is_granted=name in granted,
                    can_enable=name in config.schemas or name in granted,
                ),
            )
        )
    return tuple(toggle for _, _, toggle in sorted(rows, key=lambda row: (row[0], row[1])))


def build_workspace_toggles(
    backend_resolver: BackendResolverInterface,
    config: LatchkeyPermissionsConfig,
) -> tuple[SelfPermissionToggle, ...]:
    """Build the Other machines toggle rows: granted verbs plus revoked-but-restorable ones.

    Same union-of-rule-and-schemas construction as
    :func:`build_file_sharing_toggles`. Rows follow the verb catalog's order;
    a targeted verb gets one row per target, labelled with the target
    workspace's display name.
    """
    granted = frozenset(_self_rule_permissions(config))
    candidates = {name for name in granted if parse_workspace_permission(name) is not None}
    candidates.update(name for name in config.schemas if parse_workspace_permission(name) is not None)
    verb_order = {verb.permission: index for index, verb in enumerate(WORKSPACE_VERBS)}
    verb_by_permission = {verb.permission: verb for verb in WORKSPACE_VERBS}
    rows: list[tuple[int, str, SelfPermissionToggle]] = []
    for name in candidates:
        parsed = parse_workspace_permission(name)
        if parsed is None:
            continue
        verb_permission, target = parsed
        verb = verb_by_permission[verb_permission]
        detail = "All machines" if target is None else resolve_target_workspace_name(backend_resolver, target)
        rows.append(
            (
                verb_order[verb_permission],
                detail.lower(),
                SelfPermissionToggle(
                    permission=name,
                    label=verb.display_name,
                    detail=detail,
                    description=verb.description,
                    is_granted=name in granted,
                    can_enable=name in config.schemas or name in granted,
                ),
            )
        )
    return tuple(toggle for _, _, toggle in sorted(rows, key=lambda row: (row[0], row[1])))


@pure
def compute_connector_permissions(
    info: ServicePermissionInfo,
    current: Sequence[str],
    permission: str,
    enabled: bool,
) -> tuple[str, ...]:
    """Recompute the full permission set for one connector rule after a toggle flip.

    The result is complete (never a diff): catalog permissions in catalog
    order with the toggled one added or removed, followed by any names in the
    current grant that the catalog does not know (hand-edited entries are
    preserved verbatim rather than silently dropped). Raises
    :class:`PermissionToggleError` for a permission outside the scope's
    catalog surface -- defence-in-depth against a crafted request.
    """
    if permission not in info.permission_schemas:
        raise PermissionToggleError(
            f"Permission '{permission}' is not grantable for scope '{info.scope}'.",
        )
    membership = set(current)
    if enabled:
        membership.add(permission)
    else:
        membership.discard(permission)
    known = [name for name in info.permission_schemas if name in membership]
    unknown = [name for name in current if name not in info.permission_schemas and name in membership]
    return tuple(known + unknown)


def apply_connector_toggle(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    services_catalog: ServicesCatalog,
    latchkey: Latchkey,
    workspace_agent_id: str,
    scope: str,
    account: str,
    permission: str,
    enabled: bool,
) -> None:
    """Flip one connector permission for ``(scope, account)`` on the workspace's host.

    Reads the host file, recomputes the affected rule's complete permission
    set from the flip, and writes it back with the generated per-account
    schema (:func:`build_account_grant`). Turning the last permission off
    deletes the rule instead of leaving an empty one behind, matching the
    revoke paths. Raises :class:`PermissionToggleError` for unknown scopes /
    permissions / unresolvable workspaces; gateway failures propagate as
    :class:`LatchkeyGatewayClientError`.
    """
    info = services_catalog.get_by_scope(scope)
    if info is None:
        raise PermissionToggleError(f"Unknown scope '{scope}'.")
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot change permissions.",
        )
    path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    config = gateway_client.get_permissions_config(path)
    current: tuple[str, ...] = ()
    current_rule_key: str | None = None
    for grant in list_account_grants(config):
        if grant.scope == scope and grant.account == account:
            current = current + tuple(name for name in grant.permissions if name not in current)
            current_rule_key = grant.rule_key
    updated = compute_connector_permissions(info, current, permission, enabled)
    if tuple(updated) == current:
        return
    rule_key, granted_permissions, schemas = build_account_grant(scope, account, updated)
    if not updated:
        gateway_client.delete_permission_rule(path, current_rule_key or rule_key)
        return
    gateway_client.set_permission_rule(path, rule_key, granted_permissions, schemas)


@pure
def compute_self_permissions(
    config: LatchkeyPermissionsConfig,
    permission: str,
    enabled: bool,
) -> tuple[str, ...] | None:
    """Recompute the full ``latchkey-self`` permission list after a toggle flip.

    Only the ``minds-file-server-*`` / ``minds-workspaces-*`` names this screen
    owns may be flipped; everything else on the rule (baseline, accounts) is
    preserved verbatim, which is why the full list is recomputed here rather
    than trusted from the client. Returns ``None`` when the flip is a no-op.

    Raises :class:`PermissionToggleError` for a name outside the toggleable
    families, and for enabling a name whose schema definition is no longer in
    the file (detent would fail the entire permission check on an unresolvable
    reference, taking every ``latchkey-self`` grant down with it).
    """
    if parse_file_sharing_permission(permission) is None and parse_workspace_permission(permission) is None:
        raise PermissionToggleError(f"Permission '{permission}' is not toggleable from the permissions screen.")
    current = _self_rule_permissions(config)
    if enabled:
        if permission in current:
            return None
        if permission not in config.schemas:
            raise PermissionToggleError(
                f"Permission '{permission}' cannot be re-enabled because its definition is gone; "
                "ask the agent to request it again.",
            )
        return current + (permission,)
    if permission not in current:
        return None
    return tuple(name for name in current if name != permission)


def apply_self_toggle(
    backend_resolver: BackendResolverInterface,
    gateway_client: LatchkeyGatewayClient,
    latchkey: Latchkey,
    workspace_agent_id: str,
    permission: str,
    enabled: bool,
) -> None:
    """Flip one ``latchkey-self`` toggle (shared path / cross-workspace verb) on the workspace's host.

    Rewrites the whole ``latchkey-self`` rule with the recomputed full list
    (unrelated names preserved); a no-op flip writes nothing. Raises
    :class:`PermissionToggleError` per :func:`compute_self_permissions`;
    gateway failures propagate as :class:`LatchkeyGatewayClientError`.
    """
    host_id = resolve_workspace_host_id(backend_resolver, workspace_agent_id)
    if host_id is None:
        raise PermissionToggleError(
            f"Could not resolve host for workspace '{workspace_agent_id}'; cannot change permissions.",
        )
    path = permissions_path_for_host(latchkey.plugin_data_dir, host_id)
    updated = compute_self_permissions(gateway_client.get_permissions_config(path), permission, enabled)
    if updated is None:
        return
    gateway_client.set_permission_rule(path, SELF_SCOPE, updated)
