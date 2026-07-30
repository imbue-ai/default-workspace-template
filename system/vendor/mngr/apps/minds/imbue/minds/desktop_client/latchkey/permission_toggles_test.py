"""Unit tests for the per-workspace permission-toggle module."""

from pathlib import Path

import pytest
from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.permission_toggles import PermissionToggleError
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_connector_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_self_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import build_file_sharing_toggles
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_permissions_view
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_toggles
from imbue.minds.desktop_client.latchkey.permission_toggles import classify_permission
from imbue.minds.desktop_client.latchkey.permission_toggles import compute_connector_permissions
from imbue.minds.desktop_client.latchkey.permission_toggles import compute_self_permissions
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "permissions": [
                {"name": "slack-read-all", "description": "All reads."},
                {"name": "slack-write-all", "description": "All writes."},
                {"name": "slack-chat-read", "description": "Get permalinks."},
                {"name": "slack-chat-write", "description": "Send messages."},
            ],
        },
    ],
    "github": [
        {
            "scope": "github-rest-api",
            "display_name": "GitHub",
            "permissions": [{"name": "github-read-all"}],
        },
    ],
}

_ACCOUNT = "alice@example.com"


class _AccountsLatchkey(Latchkey):
    """``Latchkey`` double reporting a fixed set of signed-in accounts per service."""

    accounts_by_service: dict[str, list[str]] = Field(default_factory=dict)

    def auth_list(self, *, is_offline: bool = False) -> dict[str, tuple[ServiceAccountCredential, ...]]:
        del is_offline
        return {
            service: tuple(
                ServiceAccountCredential(account=account, credential_status=CredentialStatus.VALID)
                for account in accounts
            )
            for service, accounts in self.accounts_by_service.items()
        }


class _HostResolver(StaticBackendResolver):
    """Static resolver mapping every known agent to one fixed host."""

    fixed_host_id: HostId = Field(description="Host id reported for every known agent.")
    known_agent_ids: tuple[AgentId, ...] = Field(default=())

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return self.known_agent_ids

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id not in self.known_agent_ids:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=str(self.fixed_host_id))


def _catalog() -> ServicesCatalog:
    return ServicesCatalog.from_catalog_payload(_CATALOG_PAYLOAD)


def _slack_info() -> ServicePermissionInfo:
    info = _catalog().get_by_scope("slack-api")
    assert info is not None
    return info


# -- classify_permission -------------------------------------------------------


def test_classify_permission_covers_the_catalog_naming_conventions() -> None:
    """The heuristic handles verb-last, verb-first, whole-scope, bare, and wildcard names."""
    cases = [
        # (permission, scope, service, expected_heading, expected_label)
        ("slack-read-all", "slack-api", "slack", "Full access", "Read everything"),
        ("slack-write-all", "slack-api", "slack", "Full access", "Change everything"),
        ("slack-chat-read", "slack-api", "slack", "Chat", "Read chat"),
        ("slack-chat-write", "slack-api", "slack", "Chat", "Manage chat"),
        ("github-read-repos", "github-rest-api", "github", "Repos", "Read repos"),
        ("github-git-read", "github-git", "github", "Full access", "Read everything"),
        ("google-gmail-send-messages", "google-gmail-api", "google-gmail", "Messages", "Send messages"),
        ("aws-s3", "aws", "aws", "S3", "S3"),
        ("slack-search", "slack-api", "slack", "Search", "Search"),
        ("everything", "claude-ai", "claude-ai", "Full access", "Everything"),
        ("any", "slack-api", "slack", "Extras", "Everything (unrestricted)"),
    ]
    for permission, scope, service, expected_heading, expected_label in cases:
        _, heading, label = classify_permission(permission, scope, service)
        assert (heading, label) == (expected_heading, expected_label), permission


# -- compute_connector_permissions ---------------------------------------------


def test_compute_connector_permissions_returns_the_full_set_after_a_flip() -> None:
    info = _slack_info()
    enabled = compute_connector_permissions(info, ("slack-read-all",), "slack-chat-write", True)
    assert enabled == ("slack-read-all", "slack-chat-write")
    disabled = compute_connector_permissions(info, enabled, "slack-read-all", False)
    assert disabled == ("slack-chat-write",)


def test_compute_connector_permissions_keeps_catalog_order_and_unknown_names() -> None:
    """Hand-edited grants outside the catalog survive a flip verbatim, appended after catalog names."""
    info = _slack_info()
    current = ("hand-edited-extra", "slack-chat-read")
    updated = compute_connector_permissions(info, current, "slack-read-all", True)
    assert updated == ("slack-read-all", "slack-chat-read", "hand-edited-extra")


def test_compute_connector_permissions_can_empty_the_set_and_grant_the_wildcard() -> None:
    info = _slack_info()
    assert compute_connector_permissions(info, ("slack-read-all",), "slack-read-all", False) == ()
    assert compute_connector_permissions(info, (), "any", True) == ("any",)


def test_compute_connector_permissions_rejects_a_permission_outside_the_catalog() -> None:
    with pytest.raises(PermissionToggleError):
        compute_connector_permissions(_slack_info(), (), "slack-users-read", True)


# -- compute_self_permissions --------------------------------------------------


_SHARED_PATH_PERMISSION = "minds-file-server-read-/Users/me/notes"
_VERB_PERMISSION = WORKSPACE_VERBS[0].permission
_BASELINE_PERMISSION = "minds-api-proxy-call-agent-123"


def _self_config(granted: tuple[str, ...], schemas: dict[str, JsonValue] | None = None) -> LatchkeyPermissionsConfig:
    return LatchkeyPermissionsConfig(rules=({SELF_SCOPE: list(granted)},), schemas=schemas or {})


def test_compute_self_permissions_disable_preserves_unrelated_names() -> None:
    config = _self_config((_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION, _VERB_PERMISSION))
    updated = compute_self_permissions(config, _SHARED_PATH_PERMISSION, False)
    assert updated == (_BASELINE_PERMISSION, _VERB_PERMISSION)


def test_compute_self_permissions_enable_requires_the_schema_definition() -> None:
    config = _self_config((_BASELINE_PERMISSION,), schemas={_SHARED_PATH_PERMISSION: {"type": "object"}})
    updated = compute_self_permissions(config, _SHARED_PATH_PERMISSION, True)
    assert updated == (_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION)
    with pytest.raises(PermissionToggleError):
        compute_self_permissions(_self_config((_BASELINE_PERMISSION,)), _SHARED_PATH_PERMISSION, True)


def test_compute_self_permissions_is_none_for_a_no_op_flip() -> None:
    config = _self_config((_SHARED_PATH_PERMISSION,))
    assert compute_self_permissions(config, _SHARED_PATH_PERMISSION, True) is None
    assert compute_self_permissions(_self_config(()), _SHARED_PATH_PERMISSION, False) is None


def test_compute_self_permissions_rejects_non_toggleable_names() -> None:
    """Baseline / accounts names on the shared rule must not be reachable from the toggle routes."""
    with pytest.raises(PermissionToggleError):
        compute_self_permissions(_self_config((_BASELINE_PERMISSION,)), _BASELINE_PERMISSION, False)


# -- latchkey-self toggle rows -------------------------------------------------


def test_build_file_sharing_toggles_includes_revoked_but_restorable_paths() -> None:
    """A path whose schema is still in the file renders as an off toggle that can be re-enabled."""
    write_permission = "minds-file-server-write-/Users/me/notes"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
        schemas={_SHARED_PATH_PERMISSION: {"type": "object"}, write_permission: {"type": "object"}},
    )
    toggles = build_file_sharing_toggles(config)
    assert [(toggle.permission, toggle.is_granted, toggle.can_enable) for toggle in toggles] == [
        (_SHARED_PATH_PERMISSION, True, True),
        (write_permission, False, True),
    ]
    assert toggles[0].label == "/Users/me/notes"
    assert toggles[0].detail == "read"
    assert toggles[1].detail == "read and write"


def test_build_workspace_toggles_labels_verbs_and_targets() -> None:
    target_agent = str(AgentId())
    targeted_verb = next(verb for verb in WORKSPACE_VERBS if verb.is_targeted)
    untargeted_verb = next(verb for verb in WORKSPACE_VERBS if not verb.is_targeted)
    targeted_name = f"{targeted_verb.permission}-{target_agent}"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [untargeted_verb.permission, targeted_name]},),
        schemas={},
    )
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    toggles = build_workspace_toggles(resolver, config)
    by_permission = {toggle.permission: toggle for toggle in toggles}
    assert by_permission[untargeted_verb.permission].detail == "All machines"
    assert by_permission[untargeted_verb.permission].label == untargeted_verb.display_name
    # The resolver knows nothing, so the target falls back to its raw agent id.
    assert by_permission[targeted_name].detail == target_agent
    assert by_permission[targeted_name].description == targeted_verb.description


# -- build_workspace_permissions_view ------------------------------------------


def _seed_grant(plugin_dir: Path, host: HostId, scope: str, account: str, permissions: tuple[str, ...]) -> None:
    rule_key, granted, schemas = build_account_grant(scope, account, permissions)
    save_permissions(
        permissions_path_for_host(plugin_dir, host),
        LatchkeyPermissionsConfig(rules=({rule_key: list(granted)},), schemas=schemas),
    )


def test_build_workspace_permissions_view_marks_granted_toggles(tmp_path: Path) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = _AccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": [_ACCOUNT]},
    )
    _seed_grant(latchkey.plugin_data_dir, host, "slack-api", _ACCOUNT, ("slack-chat-read",))
    resolver = _HostResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,))

    view = build_workspace_permissions_view(
        backend_resolver=resolver,
        gateway_client=build_fake_gateway_client(),
        services_catalog=_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
    )

    assert view.host_id == str(host)
    # Slack is connected, so it renders as a connection; GitHub has no account
    # and no grants, so it is offered under Add connection.
    assert [connection.service_name for connection in view.connections] == ["slack"]
    assert [service.service_name for service in view.available_connections] == ["github"]
    connection = view.connections[0]
    assert connection.account == _ACCOUNT
    assert connection.is_connected
    assert connection.granted_count == 1
    toggle_states = {
        toggle.permission: toggle.is_granted for group in connection.scopes[0].groups for toggle in group.toggles
    }
    assert toggle_states["slack-chat-read"] is True
    assert toggle_states["slack-read-all"] is False
    assert toggle_states["any"] is False


def test_build_workspace_permissions_view_lists_granted_but_disconnected_accounts(tmp_path: Path) -> None:
    """Grants for an account latchkey no longer stores still render (so they can be revoked)."""
    agent_id, host = AgentId(), HostId()
    latchkey = _AccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")
    _seed_grant(latchkey.plugin_data_dir, host, "slack-api", _ACCOUNT, ("slack-read-all",))
    resolver = _HostResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,))

    view = build_workspace_permissions_view(
        backend_resolver=resolver,
        gateway_client=build_fake_gateway_client(),
        services_catalog=_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
    )

    assert [connection.service_name for connection in view.connections] == ["slack"]
    assert not view.connections[0].is_connected
    # A disconnected-but-granted service must not also appear as addable.
    assert [service.service_name for service in view.available_connections] == ["github"]


def test_build_workspace_permissions_view_rejects_unknown_workspaces(tmp_path: Path) -> None:
    latchkey = _AccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    with pytest.raises(PermissionToggleError):
        build_workspace_permissions_view(
            backend_resolver=resolver,
            gateway_client=build_fake_gateway_client(),
            services_catalog=_catalog(),
            latchkey=latchkey,
            workspace_agent_id=str(AgentId()),
        )


# -- apply_connector_toggle / apply_self_toggle --------------------------------


class _ToggleHarness(FrozenModel):
    """The typed dependency bundle the apply_* functions take, plus the host file path."""

    backend_resolver: _HostResolver = Field(description="Resolver mapping the test agent to its host.")
    gateway_client: FakeLatchkeyGatewayClient = Field(description="Fake gateway writing a real on-disk file.")
    services_catalog: ServicesCatalog = Field(description="Catalog built from the test payload.")
    latchkey: _AccountsLatchkey = Field(description="Latchkey double reporting the signed-in account.")
    workspace_agent_id: str = Field(description="The test workspace's agent id.")
    permissions_path: Path = Field(description="The host permissions file the toggles edit.")

    def apply_connector(self, scope: str, account: str, permission: str, enabled: bool) -> None:
        apply_connector_toggle(
            backend_resolver=self.backend_resolver,
            gateway_client=self.gateway_client,
            services_catalog=self.services_catalog,
            latchkey=self.latchkey,
            workspace_agent_id=self.workspace_agent_id,
            scope=scope,
            account=account,
            permission=permission,
            enabled=enabled,
        )

    def apply_self(self, permission: str, enabled: bool) -> None:
        apply_self_toggle(
            backend_resolver=self.backend_resolver,
            gateway_client=self.gateway_client,
            latchkey=self.latchkey,
            workspace_agent_id=self.workspace_agent_id,
            permission=permission,
            enabled=enabled,
        )


def _toggle_harness(tmp_path: Path, agent_id: AgentId, host: HostId) -> _ToggleHarness:
    latchkey = _AccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": [_ACCOUNT]},
    )
    return _ToggleHarness(
        backend_resolver=_HostResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,)),
        gateway_client=build_fake_gateway_client(),
        services_catalog=_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
        permissions_path=permissions_path_for_host(latchkey.plugin_data_dir, host),
    )


def test_apply_connector_toggle_writes_the_full_set_and_deletes_when_empty(tmp_path: Path) -> None:
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    rule_key = account_scope_key("slack-api", _ACCOUNT)

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=True)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ({rule_key: ["slack-chat-read"]},)
    # The generated per-account schema travels with every write.
    assert rule_key in config.schemas

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-write-all", enabled=True)
    config = load_permissions(harness.permissions_path)
    # Full set, in catalog order -- never a diff.
    assert config.rules == ({rule_key: ["slack-write-all", "slack-chat-read"]},)

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=False)
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-write-all", enabled=False)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ()


def test_apply_connector_toggle_rejects_unknown_scope(tmp_path: Path) -> None:
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    with pytest.raises(PermissionToggleError):
        harness.apply_connector(scope="nope-api", account=_ACCOUNT, permission="any", enabled=True)


def test_apply_self_toggle_rewrites_only_the_toggled_name(tmp_path: Path) -> None:
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    save_permissions(
        harness.permissions_path,
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
            schemas={_SHARED_PATH_PERMISSION: {"type": "object"}},
        ),
    )

    harness.apply_self(permission=_SHARED_PATH_PERMISSION, enabled=False)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ({SELF_SCOPE: [_BASELINE_PERMISSION]},)

    harness.apply_self(permission=_SHARED_PATH_PERMISSION, enabled=True)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},)
