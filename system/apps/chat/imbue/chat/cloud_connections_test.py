"""The cloud-connections offer: which connector names stand for a service, the list kept per account, when a
workspace counts as shared, and the conditions under which a permission card makes the offer."""

import json
import threading
from pathlib import Path

import pytest

from imbue.chat import accounts
from imbue.chat.agent_discovery import AgentInfo
from imbue.chat.cloud_connections import CONNECTED_FILENAME
from imbue.chat.cloud_connections import is_service_connected
from imbue.chat.cloud_connections import is_workspace_shared
from imbue.chat.cloud_connections import offer_for
from imbue.chat.cloud_connections import read_connected
from imbue.chat.cloud_connections import record_connected
from imbue.chat.cloud_connections import record_connected_in_background
from imbue.chat.cloud_connections import service_match_names
from imbue.chat.harnesses.claude.cloud_connections import ClaudeCloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnections
from imbue.chat.harnesses.cloud_connections import CloudConnectionsError
from imbue.chat.harnesses.harness_type import HarnessType

_GMAIL = ("google-gmail", "Gmail")


class _ScriptedConnections(CloudConnections):
    """Answers the connector list with what a test scripts: names, or a failure."""

    names: tuple[str, ...] | None = None

    def list_connected(self, account_dir: Path) -> tuple[str, ...]:
        if self.names is None:
            raise CloudConnectionsError("the provider would not say")
        return self.names

    def is_enabled(self, agent_state_dir: Path) -> bool:
        return False

    def enable(self, agent_state_dir: Path) -> None:
        pass


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
        (("slack", "Slack"), ("Slack",), True),
        (_GMAIL, ("Gmail",), True),
        (("google-calendar", "Google Calendar"), ("Google Calendar",), True),
        (("notion-mcp", "Notion (MCP)"), ("Notion",), True),
        (("github", "GitHub", "GitHub (REST API)"), ("GITHUB Enterprise",), True),
        (_GMAIL, ("Google Drive", "Google Calendar"), False),
        (("cal-com-mcp", "Cal.com (MCP)"), ("Google Calendar",), False),
        (("slack", "Slack"), (), False),
        ((), ("Slack",), False),
    ],
)
def test_a_service_is_connected_when_a_connector_is_named_like_it(
    service_names: tuple[str, ...], connected: tuple[str, ...], expected: bool
) -> None:
    """One Google connector does not stand for another Google service, which matching on the first part of the
    service's name would have it do."""
    assert is_service_connected(service_names, connected) is expected


def test_the_list_is_kept_in_the_accounts_folder_and_read_back(tmp_path: Path) -> None:
    account = _account(tmp_path)

    record_connected(account, _scripted(("Slack", "Gmail")), tmp_path)

    assert read_connected(account.id, tmp_path) == ("Slack", "Gmail")
    kept = json.loads((accounts.account_dir(account.id, tmp_path) / CONNECTED_FILENAME).read_text())
    assert kept == {"names": ["Slack", "Gmail"]}


def test_a_failed_read_keeps_nothing_and_leaves_an_earlier_list_alone(tmp_path: Path) -> None:
    account = _account(tmp_path)
    record_connected(account, _scripted(None), tmp_path)
    assert read_connected(account.id, tmp_path) == ()
    assert not (accounts.account_dir(account.id, tmp_path) / CONNECTED_FILENAME).exists()

    record_connected(account, _scripted(("Slack",)), tmp_path)
    record_connected(account, _scripted(None), tmp_path)
    assert read_connected(account.id, tmp_path) == ("Slack",)


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
