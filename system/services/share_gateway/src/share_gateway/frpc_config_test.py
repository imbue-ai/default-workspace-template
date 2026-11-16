import tomllib

from share_gateway.frpc_config import render_frpc_toml

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"


def _render(
    service_labels: list[str], media_port: int | None = None, local_media_address: str | None = "172.17.0.3"
) -> dict:
    rendered = render_frpc_toml(
        relay_host="relay-us1.infra.imbue.com",
        relay_port=7000,
        relay_token="tok-abc",
        workspace_domain=_DOMAIN,
        service_labels=service_labels,
        auth_label="auth-x7k9q2w1",
        local_https_port=8443,
        admin_port=7401,
        media_port=media_port,
        local_media_address=local_media_address,
        local_media_port=59000,
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


def test_frpc_config_pins_multi_relay_resilience_settings() -> None:
    # One relay of the fleet being down at start must not kill its tunnel
    # process for good (frp's default exits on a failed FIRST login), and the
    # tight heartbeats bound how long a wedged relay "holds" this tunnel.
    parsed = _render(["terminal-bbbb2222"])

    assert parsed["loginFailExit"] is False
    assert parsed["transport"]["heartbeatInterval"] == 10
    assert parsed["transport"]["heartbeatTimeout"] == 30


def test_frpc_config_stamps_proxy_protocol_v2_on_the_share_proxy() -> None:
    # Without the PROXY protocol stamp, caddy sees every spliced connection as
    # frpc on loopback and the real client address is unrecoverable in-workspace.
    parsed = _render(["terminal-bbbb2222"])

    assert parsed["proxies"][0]["transport"]["proxyProtocolVersion"] == "v2"


def test_frpc_config_always_claims_the_auth_label_even_with_no_services() -> None:
    parsed = _render([])
    assert parsed["proxies"][0]["customDomains"] == [f"auth-x7k9q2w1.{_DOMAIN}"]


def test_frpc_config_renders_the_screen_media_tcp_proxy_when_the_assignment_has_a_media_port() -> None:
    parsed = _render(["terminal-bbbb2222"], media_port=40123)
    proxies = parsed["proxies"]
    assert [proxy["name"] for proxy in proxies] == ["share", "screen-media"]
    media = proxies[1]
    assert media == {
        "name": "screen-media",
        "type": "tcp",
        "localIP": "172.17.0.3",
        "localPort": 59000,
        "remotePort": 40123,
    }


def test_frpc_config_renders_no_media_proxy_without_a_local_address() -> None:
    # neko reads a connection only at the interface address its ICE stack gathered, so a workspace without one
    # is not offered the relay media path rather than a proxy to loopback nothing would answer.
    parsed = _render(["terminal-bbbb2222"], media_port=40123, local_media_address=None)
    assert [proxy["name"] for proxy in parsed["proxies"]] == ["share"]


def test_frpc_config_renders_no_media_proxy_without_a_media_port() -> None:
    parsed = _render(["terminal-bbbb2222"], media_port=None)
    assert [proxy["name"] for proxy in parsed["proxies"]] == ["share"]
    assert "screen-media" not in render_frpc_toml(
        relay_host="relay-us1.infra.imbue.com",
        relay_port=7000,
        relay_token="tok-abc",
        workspace_domain=_DOMAIN,
        service_labels=[],
        auth_label="auth-x7k9q2w1",
        local_https_port=8443,
        admin_port=7401,
        media_port=None,
        local_media_address="172.17.0.3",
        local_media_port=59000,
    )
