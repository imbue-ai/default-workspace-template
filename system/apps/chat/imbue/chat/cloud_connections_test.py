"""The cloud-connections offer: which connector stands for a service or for a custom service's domain, the list
kept per account, when a workspace counts as shared, and the conditions under which a permission card makes the
offer."""

import json
import threading
from pathlib import Path

import pytest

from imbue.chat import accounts
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.cloud_connections import CONNECTED_FILENAME
from imbue.chat.cloud_connections import RequestedService
from imbue.chat.cloud_connections import find_custom_service_connector
from imbue.chat.cloud_connections import find_service_connector
from imbue.chat.cloud_connections import is_workspace_shared
from imbue.chat.cloud_connections import offer_for
from imbue.chat.cloud_connections import read_connected
from imbue.chat.cloud_connections import record_connected
from imbue.chat.cloud_connections import record_connected_in_background
from imbue.chat.cloud_connections import service_match_names
from imbue.chat.harnesses.claude.cloud_connections import ClaudeCloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.cloud_connections import CloudConnector
from imbue.chat.harnesses.harness_type import HarnessType

_GMAIL_NAMES = ("google-gmail", "Gmail")
_GMAIL = RequestedService(names=_GMAIL_NAMES)


def _named(*names: str) -> tuple[CloudConnector, ...]:
    return tuple(CloudConnector(name=name) for name in names)


class _ScriptedConnections(CloudConnections):
    """Answers the connector list with what a test scripts: connectors by name, or a failure."""

    process_started_marker_filename = "scripted_process_started"
    is_on_when_unset = False
    names: tuple[str, ...] | None = None

    def list_connected(self, account_dir: Path) -> tuple[CloudConnector, ...]:
        if self.names is None:
            raise CloudConnectionsError("the provider would not say")
        return _named(*self.names)


def _scripted(names: tuple[str, ...] | None) -> _ScriptedConnections:
    connections = _ScriptedConnections()
    connections.names = names
    return connections


def _account(home: Path, lane: str = "anthropic") -> accounts.Account:
    account_id, _path = accounts.mint_account_dir(home)
    return accounts.commit_account(account_id, lane, "Anthropic", home)


def _claude_agent(tmp_path: Path, account_id: str | None, is_enabled: bool = False) -> AgentInfo:
    """A claude agent whose host turns the connectors off, as the workspace's does."""
    host_dir = tmp_path / "host"
    agent_state_dir = host_dir / "agents" / "agent-1"
    agent_state_dir.mkdir(parents=True)
    (host_dir / "env").write_text("ENABLE_CLAUDEAI_MCP_SERVERS=false\n")
    (agent_state_dir / "env").write_text(f"MNGR_HOST_DIR={host_dir}\n")
    if is_enabled:
        ClaudeCloudConnections().enable(agent_state_dir)
        # The agent's process started after the switch was turned on.
        (agent_state_dir / ClaudeCloudConnections.process_started_marker_filename).touch()
    return AgentInfo(
        id="agent-1",
        name="chat-1",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=tmp_path / "config",
        labels={} if account_id is None else {"account": account_id},
        harness=HarnessType.CLAUDE,
    )


@pytest.mark.parametrize(
    ("service_names", "expected"),
    [
        (("slack", "Slack"), ("slack",)),
        (("google-gmail", "Gmail"), ("google gmail", "gmail")),
        (("notion-mcp", "Notion (MCP)"), ("notion",)),
        (("github", "GitHub", "GitHub (REST API)"), ("github",)),
        (("cal-com-mcp", "Cal.com (MCP)"), ("cal com", "cal.com")),
        (("x", "X"), ()),
        ((), ()),
    ],
)
def test_a_service_is_looked_for_by_its_whole_name_without_its_transport(
    service_names: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    assert service_match_names(service_names) == expected


@pytest.mark.parametrize(
    ("service_names", "connected", "expected"),
    [
        (("slack", "Slack"), ("Slack",), "Slack"),
        (_GMAIL_NAMES, ("Slack", "Gmail"), "Gmail"),
        (("google-calendar", "Google Calendar"), ("Google Calendar",), "Google Calendar"),
        (("notion-mcp", "Notion (MCP)"), ("Notion",), "Notion"),
        (("github", "GitHub", "GitHub (REST API)"), ("GITHUB Enterprise",), "GITHUB Enterprise"),
        (_GMAIL_NAMES, ("Google Drive", "Google Calendar"), None),
        (("cal-com-mcp", "Cal.com (MCP)"), ("Google Calendar",), None),
        (("slack", "Slack"), (), None),
        ((), ("Slack",), None),
    ],
)
def test_a_service_is_connected_when_a_connector_is_named_like_it(
    service_names: tuple[str, ...], connected: tuple[str, ...], expected: str | None
) -> None:
    """One Google connector does not stand for another Google service, which matching on the first part of the
    service's name would have it do."""
    assert find_service_connector(service_names, _named(*connected)) == expected


@pytest.mark.parametrize(
    ("domain", "connected", "expected"),
    [
        ("api.clickup.com", ("Slack", "ClickUp"), "ClickUp"),
        ("API.Notion.com.", ("Notion",), "Notion"),
        ("mcp.linear.app", ("Linear",), "Linear"),
        ("api.monday.com", ("monday.com",), "monday.com"),
        ("api.notion.com", ("Notion (MCP)",), "Notion (MCP)"),
        ("drive.google.com", ("Google Calendar", "Google Drive"), "Google Drive"),
        ("api.clickup.com", ("Click Up",), "Click Up"),
        ("acme.atlassian.net", ("Atlassian",), "Atlassian"),
        # A connector that names more than the domain does is for something else of the same company's.
        ("www.googleapis.com", ("Google Drive",), None),
        ("calendar.google.com", ("Google Drive",), None),
        # Whole labels only.
        ("api.dropbox.com", ("Box",), None),
        ("api.box.com", ("Dropbox",), None),
        # A name with nothing in it but generic words, or too short to mean anything, stands for no domain.
        ("api.example.com", ("API", "MCP Connector"), None),
        ("x.com", ("X",), None),
        ("api.clickup.com", (), None),
    ],
)
def test_a_custom_service_is_connected_when_a_connector_is_named_like_its_domain(
    domain: str, connected: tuple[str, ...], expected: str | None
) -> None:
    assert find_custom_service_connector(domain, None, _named(*connected)) == expected


def test_a_custom_service_is_connected_when_a_connector_is_served_from_its_address() -> None:
    """The address finds a connector whatever either is called, and is tried before any name."""
    connectors = (
        CloudConnector(name="Acme"),
        CloudConnector(name="Widgets by Acme", url="https://MCP.acme-cdn.net/mcp"),
        CloudConnector(name="Unrelated", url="not a url"),
    )

    by_endpoint = find_custom_service_connector("acme.com", "https://mcp.acme-cdn.net/v2/mcp", connectors)
    by_domain = find_custom_service_connector("mcp.acme-cdn.net", None, connectors)
    by_name = find_custom_service_connector("acme.com", "https://mcp.acme.com/mcp", connectors)

    assert by_endpoint == "Widgets by Acme"
    assert by_domain == "Widgets by Acme"
    assert by_name == "Acme"
    assert find_custom_service_connector("other.com", "https://mcp.other.com/mcp", connectors) is None


def test_the_list_is_kept_in_the_accounts_folder_and_read_back(tmp_path: Path) -> None:
    account = _account(tmp_path)

    record_connected(account, _scripted(("Slack", "Gmail")), tmp_path)

    assert read_connected(account.id, tmp_path) == _named("Slack", "Gmail")
    kept = json.loads((accounts.account_dir(account.id, tmp_path) / CONNECTED_FILENAME).read_text())
    assert kept == {"connectors": [{"name": "Slack", "url": None}, {"name": "Gmail", "url": None}]}


def test_a_failed_read_keeps_nothing_and_leaves_an_earlier_list_alone(tmp_path: Path) -> None:
    account = _account(tmp_path)
    record_connected(account, _scripted(None), tmp_path)
    assert read_connected(account.id, tmp_path) == ()
    assert not (accounts.account_dir(account.id, tmp_path) / CONNECTED_FILENAME).exists()

    record_connected(account, _scripted(("Slack",)), tmp_path)
    record_connected(account, _scripted(None), tmp_path)
    assert read_connected(account.id, tmp_path) == _named("Slack")


def test_an_unreadable_record_reads_as_no_connectors(tmp_path: Path) -> None:
    account = _account(tmp_path)
    (accounts.account_dir(account.id, tmp_path) / CONNECTED_FILENAME).write_text("not json")
    assert read_connected(account.id, tmp_path) == ()


@pytest.mark.parametrize(
    ("share_grants", "agent_grants", "expected"),
    [
        (None, None, False),
        ("[workspace]\nusers = []\nemails = []\nemail_domains = []\n", None, False),
        ('[workspace]\nusers = ["user-erin-0001"]\n', None, True),
        ('[workspace]\nemail_domains = ["example.com"]\n', None, True),
        ('[workspace]\nusers = []\n\n[services.chat]\nemails = ["erin@example.com"]\n', None, True),
        ("not [toml", None, True),
        (None, "not [toml", True),
    ],
)
def test_a_workspace_is_shared_once_anyone_is_granted_any_of_it(
    tmp_path: Path, share_grants: str | None, agent_grants: str | None, expected: bool
) -> None:
    """A document that cannot be read counts as shared: the offer is withheld rather than made to a visitor."""
    share_grants_path = tmp_path / "share_grants.toml"
    agent_grants_path = tmp_path / "agent_grants.toml"
    if share_grants is not None:
        share_grants_path.write_text(share_grants)
    if agent_grants is not None:
        agent_grants_path.write_text(agent_grants)
    assert is_workspace_shared(share_grants_path, agent_grants_path) is expected


def test_the_offer_is_made_for_a_service_the_agents_account_has_connected(tmp_path: Path) -> None:
    account = _account(tmp_path)
    record_connected(account, _scripted(("Gmail", "Slack")), tmp_path)
    agent_info = _claude_agent(tmp_path, account.id)

    offer = offer_for(agent_info, _GMAIL, is_shared=False, home=tmp_path)

    assert offer.is_offered
    assert offer.provider_name == "Anthropic"
    assert offer.connector_name == "Gmail"


def test_the_offer_is_made_for_a_custom_service_the_agents_account_has_connected(tmp_path: Path) -> None:
    account = _account(tmp_path)
    record_connected(account, _scripted(("Gmail", "ClickUp")), tmp_path)
    agent_info = _claude_agent(tmp_path, account.id)

    offer = offer_for(agent_info, RequestedService(domain="api.clickup.com"), is_shared=False, home=tmp_path)
    other = offer_for(agent_info, RequestedService(domain="api.example.com"), is_shared=False, home=tmp_path)
    unnamed = offer_for(agent_info, RequestedService(), is_shared=False, home=tmp_path)

    assert (offer.is_offered, offer.provider_name, offer.connector_name) == (True, "Anthropic", "ClickUp")
    assert not other.is_offered
    assert not unnamed.is_offered


def test_no_offer_for_a_service_the_account_has_not_connected_or_was_never_asked_about(tmp_path: Path) -> None:
    account = _account(tmp_path)
    agent_info = _claude_agent(tmp_path, account.id)
    assert not offer_for(agent_info, _GMAIL, is_shared=False, home=tmp_path).is_offered

    record_connected(account, _scripted(("Slack",)), tmp_path)
    assert not offer_for(agent_info, _GMAIL, is_shared=False, home=tmp_path).is_offered


def test_no_offer_in_a_shared_workspace(tmp_path: Path) -> None:
    account = _account(tmp_path)
    record_connected(account, _scripted(("Gmail",)), tmp_path)
    agent_info = _claude_agent(tmp_path, account.id)
    assert not offer_for(agent_info, _GMAIL, is_shared=True, home=tmp_path).is_offered


def test_no_offer_to_an_agent_that_already_loads_its_connections(tmp_path: Path) -> None:
    account = _account(tmp_path)
    record_connected(account, _scripted(("Gmail",)), tmp_path)
    agent_info = _claude_agent(tmp_path, account.id, is_enabled=True)
    assert not offer_for(agent_info, _GMAIL, is_shared=False, home=tmp_path).is_offered


def test_no_offer_to_an_agent_on_no_account_or_on_a_harness_without_cloud_connections(tmp_path: Path) -> None:
    account = _account(tmp_path)
    record_connected(account, _scripted(("Gmail",)), tmp_path)
    assert not offer_for(_claude_agent(tmp_path, None), _GMAIL, is_shared=False, home=tmp_path).is_offered

    signed_out = _claude_agent(tmp_path / "other", "0" * 32)
    assert not offer_for(signed_out, _GMAIL, is_shared=False, home=tmp_path).is_offered

    pi_agent = AgentInfo(
        id="agent-2",
        name="chat-2",
        state="RUNNING",
        agent_state_dir=tmp_path / "pi",
        claude_config_dir=tmp_path / "config",
        labels={"account": account.id},
        harness=HarnessType.PI_CODING,
    )
    assert not offer_for(pi_agent, _GMAIL, is_shared=False, home=tmp_path).is_offered


def test_a_sign_in_on_a_harness_without_cloud_connections_starts_nothing() -> None:
    account_id, _path = accounts.mint_account_dir()
    account = accounts.commit_account(account_id, "opencode-go", "Opencode Go")
    threads_before = set(threading.enumerate())

    record_connected_in_background(account)

    assert set(threading.enumerate()) <= threads_before
    assert read_connected(account.id) == ()


def test_a_sign_in_that_left_no_subscription_credential_records_nothing() -> None:
    """A claude account signed in with an API key: the background read finds no claude.ai sign-in and keeps nothing."""
    account_id, _path = accounts.mint_account_dir()
    account = accounts.commit_account(account_id, "anthropic", "Anthropic")

    record_connected_in_background(account)

    for thread in threading.enumerate():
        if thread.name == f"cloud-connections-{account.id[:8]}":
            thread.join(timeout=10.0)
    assert read_connected(account.id) == ()
    assert not (accounts.account_dir(account.id) / CONNECTED_FILENAME).exists()
