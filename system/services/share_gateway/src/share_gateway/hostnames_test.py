from share_gateway.hostnames import service_for_host
from share_gateway.hostnames import workspace_origins_allow

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"


def test_bare_domain_is_the_shell() -> None:
    assert service_for_host(_DOMAIN, _DOMAIN) == (True, None)
    assert service_for_host(_DOMAIN.upper(), _DOMAIN) == (True, None)
    assert service_for_host(f"{_DOMAIN}:8443", _DOMAIN) == (True, None)


def test_single_label_selects_the_service() -> None:
    assert service_for_host(f"web.{_DOMAIN}", _DOMAIN) == (True, "web")
    assert service_for_host(f"terminal.{_DOMAIN}:443", _DOMAIN) == (True, "terminal")


def test_deeper_labels_route_to_the_adjacent_service() -> None:
    assert service_for_host(f"a.b.web.{_DOMAIN}", _DOMAIN) == (True, "web")


def test_foreign_hosts_are_rejected() -> None:
    assert service_for_host("evil.example.com", _DOMAIN) == (False, None)
    assert service_for_host(f"evil-{_DOMAIN}", _DOMAIN) == (False, None)
    assert service_for_host("", _DOMAIN) == (False, None)


def test_workspace_origins_allow_own_origins_only() -> None:
    assert workspace_origins_allow(f"https://{_DOMAIN}", _DOMAIN) is True
    assert workspace_origins_allow(f"https://web.{_DOMAIN}", _DOMAIN) is True
    assert workspace_origins_allow("https://evil.example.com", _DOMAIN) is False
    assert workspace_origins_allow(f"http://{_DOMAIN}", _DOMAIN) is False
    assert workspace_origins_allow("null", _DOMAIN) is False
    assert workspace_origins_allow("", _DOMAIN) is False
