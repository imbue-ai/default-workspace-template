"""Integration tests for the workspace Permissions pane and its toggle routes."""

from pathlib import Path

from flask.testing import FlaskClient
from pydantic import Field
from pydantic import JsonValue

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import create_latchkey_predefined_permission_request_event
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "permissions": [
                {"name": "slack-read-all", "description": "All reads."},
                {"name": "slack-write-all", "description": "All writes."},
                {"name": "slack-chat-read", "description": "Get permalinks."},
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

_SHARED_PATH_PERMISSION = "minds-file-server-read-/Users/me/notes"
_BASELINE_PERMISSION = "minds-api-proxy-call-agent-123"


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


class _WorkspaceResolver(StaticBackendResolver):
    """Static resolver that reports active machines mapped to hosts, with names."""

    host_by_agent: dict[str, str] = Field(default_factory=dict)
    name_by_agent: dict[str, str] = Field(default_factory=dict)

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(agent) for agent in self.host_by_agent)

    def list_active_workspace_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(agent) for agent in self.host_by_agent)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        host = self.host_by_agent.get(str(agent_id))
        if host is None:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=host)

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.name_by_agent.get(str(agent_id))


def _build_handler(tmp_path: Path, accounts_by_service: dict[str, list[str]]) -> LatchkeyPermissionGrantHandler:
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=_AccountsLatchkey(
            latchkey_directory=tmp_path,
            latchkey_binary="/nonexistent",
            accounts_by_service=accounts_by_service,
        ),
        services_catalog=ServicesCatalog.from_catalog_payload(_CATALOG_PAYLOAD),
        mngr_message_sender=MngrMessageSender(
            mngr_caller=RecordingMngrCaller(),
            concurrency_group=ConcurrencyGroup(name="workspace-permissions-routes-test-unused"),
            retry_delays_seconds=(),
        ),
        gateway_client=build_fake_gateway_client(),
    )


def _build_client(
    tmp_path: Path,
    handler: LatchkeyPermissionGrantHandler,
    agent: str,
    host: HostId,
    request_inbox: RequestInbox | None = None,
) -> FlaskClient:
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = _WorkspaceResolver(
        url_by_agent_and_service={},
        host_by_agent={agent: str(host)},
        name_by_agent={agent: "My Machine"},
    )
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path),
        request_inbox=request_inbox if request_inbox is not None else RequestInbox(),
        request_event_handlers=(handler,),
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client


def _seed_account_grant(handler: LatchkeyPermissionGrantHandler, host: HostId, permissions: tuple[str, ...]) -> Path:
    path = permissions_path_for_host(handler.latchkey.plugin_data_dir, host)
    rule_key, granted, schemas = build_account_grant("slack-api", _ACCOUNT, permissions)
    save_permissions(path, LatchkeyPermissionsConfig(rules=({rule_key: list(granted)},), schemas=schemas))
    return path


def test_options_page_renders_permission_toggles(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    _seed_account_grant(handler, host, ("slack-chat-read",))
    client = _build_client(tmp_path, handler, agent, host)

    response = client.get(f"/workspace/{agent}/options?tab=permissions")

    assert response.status_code == 200
    body = response.text
    # The pane, its nav, and the granted toggle all render.
    assert 'data-wsopt-panel="permissions"' in body
    assert 'id="ws-permissions"' in body
    assert 'data-perm-scope="slack-api"' in body
    read_idx = body.find('data-perm-permission="slack-chat-read"')
    assert read_idx != -1
    tag_start = body.rfind("<button", 0, read_idx)
    tag_end = body.find(">", read_idx)
    assert 'aria-checked="true"' in body[tag_start:tag_end]
    # An ungranted permission renders as an off toggle.
    write_idx = body.find('data-perm-permission="slack-write-all"')
    assert write_idx != -1
    write_tag = body[body.rfind("<button", 0, write_idx) : body.find(">", write_idx)]
    assert 'aria-checked="false"' in write_tag
    # GitHub has no account, so it is offered under Add connection.
    assert 'data-perm-panel="add-connection"' in body
    assert 'data-service-name="github"' in body
    # The titlebar carries the Permissions icon-tab on this page.
    assert 'id="ws-tab-permissions"' in body


def test_options_page_shows_waiting_strip_for_this_workspaces_pending_requests(tmp_path: Path) -> None:
    """Pending requests filed by this workspace's agents lead the Permissions pane.

    The strip shows the request's service title + the agent's reason, keyed
    by the request id (the row opens the review popup); requests from other
    workspaces are excluded, and the strip is absent when nothing is pending.
    """
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    request = create_latchkey_predefined_permission_request_event(
        agent_id=agent,
        scope="slack-api",
        permissions=("slack-read-all",),
        rationale="Reading the team channel to draft the digest.",
    )
    inbox = RequestInbox().add_request(request)
    client = _build_client(tmp_path, handler, agent, host, request_inbox=inbox)

    response = client.get(f"/workspace/{agent}/options?tab=permissions")

    assert response.status_code == 200
    body = response.text
    assert "Waiting on you" in body
    assert f'data-request-id="{request.event_id}"' in body
    assert "Reading the team channel to draft the digest." in body
    # The row leads with the catalog display name for the requested scope.
    strip = body[body.find('id="ws-perm-waiting"') : body.find('id="ws-permissions"')]
    assert "Slack" in strip

    # With nothing pending, the strip is absent entirely.
    empty_client = _build_client(tmp_path, _build_handler(tmp_path, {"slack": [_ACCOUNT]}), agent, host)
    empty_body = empty_client.get(f"/workspace/{agent}/options?tab=permissions").text
    assert "Waiting on you" not in empty_body


def test_options_page_renders_unavailable_notice_without_a_handler(tmp_path: Path) -> None:
    """With no latchkey handler wired, the pane degrades to its unavailable notice."""
    agent, host = str(AgentId()), HostId()
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    resolver = _WorkspaceResolver(
        url_by_agent_and_service={},
        host_by_agent={agent: str(host)},
        name_by_agent={agent: "My Machine"},
    )
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=resolver,
        http_client=None,
        paths=WorkspacePaths(data_dir=tmp_path),
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))

    response = client.get(f"/workspace/{agent}/options?tab=permissions")

    assert response.status_code == 200
    assert "Permissions can't be loaded right now" in response.text


def test_connector_toggle_enable_writes_the_full_set(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    path = _seed_account_grant(handler, host, ("slack-chat-read",))
    client = _build_client(tmp_path, handler, agent, host)

    response = client.post(
        f"/workspace/{agent}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-read-all", "enabled": True},
    )

    assert response.status_code == 200
    config = load_permissions(path)
    rule_key = account_scope_key("slack-api", _ACCOUNT)
    # The rule now carries the COMPLETE set (catalog order), not a diff.
    assert config.rules == ({rule_key: ["slack-read-all", "slack-chat-read"]},)
    assert rule_key in config.schemas


def test_connector_toggle_disabling_the_last_permission_deletes_the_rule(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    path = _seed_account_grant(handler, host, ("slack-chat-read",))
    client = _build_client(tmp_path, handler, agent, host)

    response = client.post(
        f"/workspace/{agent}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-chat-read", "enabled": False},
    )

    assert response.status_code == 200
    assert load_permissions(path).rules == ()


def test_connector_toggle_validates_its_body(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    client = _build_client(tmp_path, handler, agent, host)

    missing_account = client.post(
        f"/workspace/{agent}/permissions/connector-toggle",
        json={"scope": "slack-api", "permission": "slack-read-all", "enabled": True},
    )
    assert missing_account.status_code == 400
    non_boolean = client.post(
        f"/workspace/{agent}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-read-all", "enabled": "yes"},
    )
    assert non_boolean.status_code == 400
    unknown_permission = client.post(
        f"/workspace/{agent}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-users-read", "enabled": True},
    )
    assert unknown_permission.status_code == 400


def test_connector_toggle_requires_authentication(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    client = _build_client(tmp_path, handler, agent, host)
    client.delete_cookie(SESSION_COOKIE_NAME)

    response = client.post(
        f"/workspace/{agent}/permissions/connector-toggle",
        json={"scope": "slack-api", "account": _ACCOUNT, "permission": "slack-read-all", "enabled": True},
    )

    assert response.status_code == 403


def test_self_toggle_preserves_unrelated_latchkey_self_permissions(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    path = permissions_path_for_host(handler.latchkey.plugin_data_dir, host)
    schemas: dict[str, JsonValue] = {_SHARED_PATH_PERMISSION: {"type": "object"}}
    save_permissions(
        path,
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
            schemas=schemas,
        ),
    )
    client = _build_client(tmp_path, handler, agent, host)

    disable = client.post(
        f"/workspace/{agent}/permissions/self-toggle",
        json={"permission": _SHARED_PATH_PERMISSION, "enabled": False},
    )
    assert disable.status_code == 200
    assert load_permissions(path).rules == ({SELF_SCOPE: [_BASELINE_PERMISSION]},)

    enable = client.post(
        f"/workspace/{agent}/permissions/self-toggle",
        json={"permission": _SHARED_PATH_PERMISSION, "enabled": True},
    )
    assert enable.status_code == 200
    assert load_permissions(path).rules == ({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},)


def test_self_toggle_rejects_baseline_names_and_missing_schemas(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    path = permissions_path_for_host(handler.latchkey.plugin_data_dir, host)
    save_permissions(path, LatchkeyPermissionsConfig(rules=({SELF_SCOPE: [_BASELINE_PERMISSION]},), schemas={}))
    client = _build_client(tmp_path, handler, agent, host)

    baseline = client.post(
        f"/workspace/{agent}/permissions/self-toggle",
        json={"permission": _BASELINE_PERMISSION, "enabled": False},
    )
    assert baseline.status_code == 400
    # Baseline permissions cannot be flipped from this surface.
    assert load_permissions(path).rules == ({SELF_SCOPE: [_BASELINE_PERMISSION]},)

    missing_schema = client.post(
        f"/workspace/{agent}/permissions/self-toggle",
        json={"permission": _SHARED_PATH_PERMISSION, "enabled": True},
    )
    assert missing_schema.status_code == 400


def test_connector_revoke_all_removes_the_accounts_rules(tmp_path: Path) -> None:
    agent, host = str(AgentId()), HostId()
    handler = _build_handler(tmp_path, {"slack": [_ACCOUNT]})
    path = _seed_account_grant(handler, host, ("slack-read-all", "slack-chat-read"))
    client = _build_client(tmp_path, handler, agent, host)

    response = client.post(
        f"/workspace/{agent}/permissions/connector-revoke-all",
        json={"service_name": "slack", "account": _ACCOUNT},
    )

    assert response.status_code == 200
    assert load_permissions(path).rules == ()
