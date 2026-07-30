import tomllib

from share_gateway.frpc_config import render_frpc_toml

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"


def test_frpc_config_claims_exactly_the_share_domains() -> None:
    rendered = render_frpc_toml(
        relay_host="relay-us1.infra.imbue.com",
        relay_port=7000,
        relay_token="tok-abc",
        workspace_domain=_DOMAIN,
        local_https_port=8443,
    )

    parsed = tomllib.loads(rendered)
    assert parsed["serverAddr"] == "relay-us1.infra.imbue.com"
    assert parsed["serverPort"] == 7000
    assert parsed["transport"]["tls"]["enable"] is True
    assert parsed["metadatas"]["relay_token"] == "tok-abc"
    proxies = parsed["proxies"]
    assert len(proxies) == 1
    assert proxies[0]["type"] == "https"
    assert proxies[0]["localPort"] == 8443
    assert proxies[0]["customDomains"] == [_DOMAIN, f"*.{_DOMAIN}"]
