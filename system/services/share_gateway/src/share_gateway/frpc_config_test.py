import tomllib

from share_gateway.frpc_config import render_frpc_toml

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"


def _render(service_labels: list[str]) -> dict:
    rendered = render_frpc_toml(
        relay_host="relay-us1.infra.imbue.com",
        relay_port=7000,
        relay_token="tok-abc",
        workspace_domain=_DOMAIN,
        service_labels=service_labels,
        auth_label="auth-x7k9q2w1",
        local_https_port=8443,
        admin_port=7401,
    )
    return tomllib.loads(rendered)


def test_frpc_config_claims_exactly_the_service_and_auth_labels() -> None:
    parsed = _render(["terminal-bbbb2222", "system_interface-aaaa1111"])

    assert parsed["serverAddr"] == "relay-us1.infra.imbue.com"
    assert parsed["serverPort"] == 7000
    assert parsed["transport"]["tls"]["enable"] is True
    assert parsed["webServer"]["port"] == 7401
    assert parsed["metadatas"]["relay_token"] == "tok-abc"
    proxies = parsed["proxies"]
    assert len(proxies) == 1
    assert proxies[0]["type"] == "https"
    assert proxies[0]["localPort"] == 8443
    # Explicit per-label claims (sorted), plus the auth label; never the
    # wildcard and never the bare domain.
    assert proxies[0]["customDomains"] == [
        f"auth-x7k9q2w1.{_DOMAIN}",
        f"system_interface-aaaa1111.{_DOMAIN}",
        f"terminal-bbbb2222.{_DOMAIN}",
    ]
    assert f"*.{_DOMAIN}" not in proxies[0]["customDomains"]
    assert _DOMAIN not in proxies[0]["customDomains"]


def test_frpc_config_stamps_proxy_protocol_v2_on_the_share_proxy() -> None:
    # Without the PROXY protocol stamp, caddy sees every spliced connection as
    # frpc on loopback and the real client address is unrecoverable in-workspace.
    parsed = _render(["terminal-bbbb2222"])

    assert parsed["proxies"][0]["transport"]["proxyProtocolVersion"] == "v2"


def test_frpc_config_always_claims_the_auth_label_even_with_no_services() -> None:
    parsed = _render([])
    assert parsed["proxies"][0]["customDomains"] == [f"auth-x7k9q2w1.{_DOMAIN}"]
