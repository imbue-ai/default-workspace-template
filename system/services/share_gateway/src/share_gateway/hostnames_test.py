from share_gateway.hostnames import service_for_host
from share_gateway.hostnames import workspace_origins_allow

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"
_AUTH_LABEL = "auth-x7k9q2w1"
# label -> service name, as built from apps.toml.
_LABELS = {"web-1a2b3c4d": "web", "terminal-9z8y7x6w": "terminal", "system_interface-abcd1234": "system_interface"}


def test_bare_domain_does_not_route() -> None:
    # The bare workspace domain is the CT-visible cert name and is deliberately
    # unclaimed on the relay -- it is not one of our routable origins.
    assert service_for_host(_DOMAIN, _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)
    assert service_for_host(f"{_DOMAIN}:8443", _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)


def test_known_label_selects_the_service_by_name() -> None:
    assert service_for_host(f"web-1a2b3c4d.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) == (True, "web")
    assert service_for_host(f"terminal-9z8y7x6w.{_DOMAIN}:443", _DOMAIN, _LABELS, _AUTH_LABEL) == (True, "terminal")
    # The label is what maps -- the underlying service name is not usable as a label.
    assert service_for_host(f"web.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)


def test_auth_label_is_ours_but_not_a_service() -> None:
    assert service_for_host(f"{_AUTH_LABEL}.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) == (True, None)


def test_unknown_and_deeper_labels_are_rejected() -> None:
    # An unregistered label is not claimed on the relay, so it is not ours.
    assert service_for_host(f"unknown-00000000.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)
    # Only single-label origins are claimed; a deeper chain is not ours.
    assert service_for_host(f"a.web-1a2b3c4d.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)


def test_foreign_hosts_are_rejected() -> None:
    assert service_for_host("evil.example.com", _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)
    assert service_for_host(f"evil-{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)
    assert service_for_host("", _DOMAIN, _LABELS, _AUTH_LABEL) == (False, None)


def test_workspace_origins_allow_own_origins_only() -> None:
    assert workspace_origins_allow(f"https://web-1a2b3c4d.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) is True
    assert workspace_origins_allow(f"https://{_AUTH_LABEL}.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) is True
    # The bare domain is not a routable origin.
    assert workspace_origins_allow(f"https://{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) is False
    assert workspace_origins_allow("https://evil.example.com", _DOMAIN, _LABELS, _AUTH_LABEL) is False
    assert workspace_origins_allow(f"http://web-1a2b3c4d.{_DOMAIN}", _DOMAIN, _LABELS, _AUTH_LABEL) is False
    assert workspace_origins_allow("null", _DOMAIN, _LABELS, _AUTH_LABEL) is False
    assert workspace_origins_allow("", _DOMAIN, _LABELS, _AUTH_LABEL) is False
