import pytest
from pydantic import AnyHttpUrl

from imbue.imbue_common.model_update import to_update
from imbue.share_relay.config_render import render_frps_toml
from imbue.share_relay.config_render import render_nftables_conf
from imbue.share_relay.config_render import render_port_80_redirect_caddyfile
from imbue.share_relay.data_types import RelayConfiguration
from imbue.share_relay.primitives import ContentDomain
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.primitives import RelayId


def _config() -> RelayConfiguration:
    return RelayConfiguration(
        relay_id=RelayId("relay-" + "e" * 16),
        region=RegionCode("us1"),
        content_domain=ContentDomain("imbueminds.com"),
        plugin_auth_url=AnyHttpUrl("https://connector.example.com/frps/auth"),
    )


def test_frps_toml_is_sni_passthrough_and_has_no_tls_termination() -> None:
    rendered = render_frps_toml(_config())
    # SNI-passthrough mode is exactly ``vhostHTTPSPort`` with no tls block: if a
    # tls block ever appears the relay would terminate TLS and become a MITM.
    assert "vhostHTTPSPort = 443" in rendered
    assert "[tls]" not in rendered
    assert "tls_cert" not in rendered


def test_frps_toml_authorizes_only_the_tunnel_gating_ops() -> None:
    rendered = render_frps_toml(_config())
    assert 'ops = ["Login", "NewProxy", "Ping"]' in rendered
    # Visitor connections must NOT be authorized per-connection (that would put
    # the connector in every visitor's request path).
    assert "NewUserConn" not in rendered


def test_frps_toml_points_the_plugin_at_the_connector() -> None:
    """The auth URL is split into origin + path: frps builds the callback URL as addr + path."""
    rendered = render_frps_toml(_config())
    assert 'addr = "https://connector.example.com"' in rendered
    # The relay's own id is appended so the connector can attribute callbacks.
    assert 'path = "/frps/auth/relay-' + "e" * 16 + '"' in rendered
    # Without tlsVerify frp skips certificate verification on https plugin
    # addrs, exposing the shared auth secret to an on-path attacker.
    assert "tlsVerify = true" in rendered


@pytest.mark.parametrize(
    "bad_url",
    ["https://connector.example.com/frps/auth?secret=abc", "https://connector.example.com/frps/auth#frag"],
)
def test_plugin_auth_url_rejects_query_and_fragment(bad_url: str) -> None:
    # The renderer keeps only origin + path, so a query/fragment (e.g. a secret
    # passed as ?secret=...) would be silently dropped from the frps config.
    with pytest.raises(ValueError, match="query string or fragment"):
        RelayConfiguration(
            relay_id=RelayId("relay-" + "e" * 16),
            region=RegionCode("us1"),
            content_domain=ContentDomain("imbueminds.com"),
            plugin_auth_url=AnyHttpUrl(bad_url),
        )


def test_nftables_conf_caps_rate_and_concurrency_on_the_vhost_port() -> None:
    rendered = render_nftables_conf(_config())
    assert "tcp dport 443" in rendered
    assert "flush ruleset" in rendered
    # Each limit needs one rule per address family: in an inet table an
    # `ip saddr` rule never matches IPv6 packets, so IPv4-only rules would let
    # IPv6 clients bypass the guard entirely.
    assert "ip saddr limit rate over 20/second burst 40 packets" in rendered
    assert "ip6 saddr limit rate over 20/second burst 40 packets" in rendered
    assert "ip saddr ct count over 100" in rendered
    assert "ip6 saddr ct count over 100" in rendered


def test_nftables_conf_respects_overridden_limits() -> None:
    base = _config()
    config = base.model_copy_update(
        to_update(base.field_ref().max_new_connections_per_second_per_ip, 5),
        to_update(base.field_ref().max_new_connections_burst_per_ip, 9),
        to_update(base.field_ref().max_concurrent_connections_per_ip, 50),
    )
    rendered = render_nftables_conf(config)
    assert "ip saddr limit rate over 5/second burst 9 packets" in rendered
    assert "ip6 saddr limit rate over 5/second burst 9 packets" in rendered
    assert "ip saddr ct count over 50" in rendered
    assert "ip6 saddr ct count over 50" in rendered


def test_port_80_caddyfile_redirects_to_https_preserving_host_and_path() -> None:
    rendered = render_port_80_redirect_caddyfile(_config())
    assert ":80 {" in rendered
    assert "redir https://{host}{uri} permanent" in rendered


def test_region_domain_and_vhost_wildcard() -> None:
    config = _config()
    assert config.region_domain == "us1.imbueminds.com"
    # The wildcard is one label deep, which is what the universal-cert-free
    # SNI passthrough needs: frps matches ``<anything>.us1.imbueminds.com``.
    assert config.vhost_wildcard == "*.us1.imbueminds.com"


@pytest.mark.parametrize("bad_region", ["US1", "us_1", "-us1", "us1-", "us..1", ""])
def test_region_code_rejects_non_dns_labels(bad_region: str) -> None:
    with pytest.raises(ValueError):
        RegionCode(bad_region)


@pytest.mark.parametrize("good_region", ["us1", "us2", "eu1", "dev-josh-1", "af2"])
def test_region_code_accepts_dns_labels(good_region: str) -> None:
    assert RegionCode(good_region) == good_region
