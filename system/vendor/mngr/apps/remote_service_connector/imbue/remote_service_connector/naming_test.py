import pytest

from imbue.remote_service_connector.errors import InvalidTunnelComponentError
from imbue.remote_service_connector.errors import TunnelComponentTooLongError
from imbue.remote_service_connector.naming import extract_service_name
from imbue.remote_service_connector.naming import extract_user_id_prefix_from_tunnel_name
from imbue.remote_service_connector.naming import make_hostname
from imbue.remote_service_connector.naming import make_tunnel_name


def test_make_tunnel_name_format() -> None:
    assert make_tunnel_name("alice", "agent1") == "alice--agent1"


def test_make_tunnel_name_allows_single_hyphen_in_agent_id() -> None:
    assert make_tunnel_name("alice", "agent-abc123") == "alice--abc123"


def test_make_tunnel_name_rejects_double_hyphen_in_user_id_prefix() -> None:
    with pytest.raises(InvalidTunnelComponentError, match="User ID prefix"):
        make_tunnel_name("alice--bob", "agent1")


def test_make_tunnel_name_truncates_agent_id() -> None:
    result = make_tunnel_name("alice", "agent--1")
    assert result == "alice---1"


def test_make_hostname_format() -> None:
    assert make_hostname("web", "agent1", "alice", "example.com") == "web--agent1--alice.example.com"


def test_extract_service_name_from_hostname() -> None:
    assert extract_service_name("web--agent1--alice.example.com", "agent1", "alice", "example.com") == "web"


def test_extract_service_name_returns_none_for_non_matching() -> None:
    assert extract_service_name("other.example.com", "agent1", "alice", "example.com") is None


def test_extract_user_id_prefix_from_tunnel_name() -> None:
    assert extract_user_id_prefix_from_tunnel_name("alice--agent1") == "alice"


def test_tunnel_component_too_long_error_message() -> None:
    with pytest.raises(TunnelComponentTooLongError) as exc_info:
        raise TunnelComponentTooLongError("User ID prefix", "toolong", 5)
    assert "User ID prefix" in str(exc_info.value)
    assert "toolong" in str(exc_info.value)
    assert "5" in str(exc_info.value)
