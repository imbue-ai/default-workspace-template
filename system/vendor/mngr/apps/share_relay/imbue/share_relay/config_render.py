"""Pure renderers for a relay host's on-disk config: frps, nftables, the :80 redirector.

A relay never terminates TLS and holds no per-share state; these renderers turn
a :class:`RelayConfiguration` into the exact text files a fresh VPS needs. They
are pure so the deploy CLI can diff / preview them and so they are unit-testable
without touching a host.
"""

from typing import Final
from urllib.parse import urlsplit

from imbue.imbue_common.pure import pure
from imbue.share_relay.data_types import RelayConfiguration

# The frps server-plugin protocol subscribes a plugin to named operations; we
# authorize the two that gate a workspace tunnel -- ``Login`` (a frpc
# connecting / reconnecting) and ``NewProxy`` (that client registering its
# hostname claim) -- plus ``Ping`` (the workspace's 10s heartbeat), which is
# what lets the connector sever a LIVE tunnel: rejecting a heartbeat makes
# frpc close its session, and the follow-up Login is refused for any share
# that is no longer active (unshared or account-suspended). Visitor
# connections and requests never call the connector.
_PLUGIN_OPS: Final[tuple[str, ...]] = ("Login", "NewProxy", "Ping")


@pure
def render_frps_toml(config: RelayConfiguration) -> str:
    """Render ``frps.toml`` for SNI-passthrough vhost routing + connector auth.

    ``vhostHTTPSPort`` puts frps in TLS-passthrough mode: it routes by the
    ClientHello SNI without terminating TLS. The server plugin authorizes every
    ``Login`` / ``NewProxy`` against the connector, so an frpc can only register
    the workspace hostnames its relay token is allowed to claim.

    frps builds the plugin callback URL by concatenating ``addr`` + ``path``,
    so the configured auth URL is split into its origin (``addr``) and its URL
    path (``path``) rather than rendered whole. The shared auth secret rides as
    the addr's URL userinfo: Go's HTTP client turns userinfo into an
    ``Authorization: Basic`` header (secret as the username), which keeps the
    secret out of the URL path that the connector's access logs record (the
    behavior is pinned by the frp_verification harness). The relay's own id is
    the final path segment so the connector can attribute every callback (and
    the per-relay tunnel-login stamps) to this relay.
    """
    ops = ", ".join(f'"{op}"' for op in _PLUGIN_OPS)
    auth_url = urlsplit(str(config.plugin_auth_url))
    plugin_addr = f"{auth_url.scheme}://{config.plugin_auth_secret.get_secret_value()}@{auth_url.netloc}"
    plugin_path = f"{auth_url.path.rstrip('/')}/{config.relay_id}"
    return f"""\
# Rendered by imbue.share_relay -- do not edit on the host; re-render and redeploy.
# frps in SNI-passthrough mode for region {config.region} ({config.region_domain}).

bindPort = {config.tunnel_control_port}
vhostHTTPSPort = {config.vhost_https_port}

# TLS is terminated inside each workspace, never here: frps only reads the SNI
# and splices ciphertext. Do not add a tls block.

[[httpPlugins]]
name = "connector-auth"
addr = "{plugin_addr}"
path = "{plugin_path}"
ops = [{ops}]
# Verify the connector's certificate: frp defaults to InsecureSkipVerify for
# https plugin addrs, and this channel carries the shared auth secret and the
# Login/NewProxy authorization decisions.
tlsVerify = true
"""


@pure
def render_nftables_conf(config: RelayConfiguration) -> str:
    """Render an nftables ruleset: per-source-IP rate + concurrency limits on the vhost port.

    This is tier-2 abuse prevention (per the sharing-redesign plan): it caps a
    single source IP's new-connection rate and concurrent connections before
    frps sees them. Per-workspace bandwidth quotas (tier 3) are deferred and
    enforced connector-side, not here.
    """
    return f"""\
#!/usr/sbin/nft -f
# Rendered by imbue.share_relay -- do not edit on the host; re-render and redeploy.
# Tier-2 abuse guard for region {config.region}: per-source-IP limits on the
# SNI-passthrough vhost port. TLS terminates in the workspace, so the relay
# does no crypto and these limits bound only cheap splice connections.

flush ruleset

table inet share_relay {{
    # Connlimit sets must not declare element timeouts: the kernel's
    # nf_conncount expression rejects timed elements ("Operation not
    # supported"). Entries are reaped when their connection count drops.
    set per_ip_conns {{
        type ipv4_addr
        flags dynamic
    }}

    set per_ip6_conns {{
        type ipv6_addr
        flags dynamic
    }}

    chain input {{
        type filter hook input priority filter; policy accept;

        # Rate-limit new connections to the vhost port per source IP. In an
        # inet table an `ip saddr` rule never matches IPv6 packets (and vice
        # versa), so each limit needs one rule per address family.
        tcp dport {config.vhost_https_port} ct state new meter ratemeter {{ ip saddr limit rate over {config.max_new_connections_per_second_per_ip}/second burst {config.max_new_connections_burst_per_ip} packets }} drop
        tcp dport {config.vhost_https_port} ct state new meter ratemeter6 {{ ip6 saddr limit rate over {config.max_new_connections_per_second_per_ip}/second burst {config.max_new_connections_burst_per_ip} packets }} drop

        # Cap concurrent connections to the vhost port per source IP.
        tcp dport {config.vhost_https_port} ct state new add @per_ip_conns {{ ip saddr ct count over {config.max_concurrent_connections_per_ip} }} drop
        tcp dport {config.vhost_https_port} ct state new add @per_ip6_conns {{ ip6 saddr ct count over {config.max_concurrent_connections_per_ip} }} drop
    }}
}}
"""


@pure
def render_port_80_redirect_caddyfile(config: RelayConfiguration) -> str:
    """Render a Caddyfile for the dumb :80 -> https redirector.

    SNI passthrough is 443-only; a bare ``http://<host>`` link would otherwise
    hang. This same-host redirector answers :80 with a 301 to the https scheme,
    preserving host and path. It does no routing and terminates no workspace
    TLS -- it only ever sees plaintext :80 requests to the relay itself.
    """
    return f"""\
# Rendered by imbue.share_relay -- do not edit on the host; re-render and redeploy.
# :80 -> https redirector for region {config.region}. Routes nothing; the real
# workspace traffic is SNI-passthrough on {config.vhost_https_port}.
:80 {{
    redir https://{{host}}{{uri}} permanent
}}
"""


@pure
def render_all_artifacts(config: RelayConfiguration) -> dict[str, str]:
    """All rendered relay config artifacts, keyed by their on-disk basename.

    The single source of truth for which config files a relay carries: the
    render CLI writes exactly these, and the SSH deploy stages them (the
    basenames must appear in ``remote_install.REMOTE_ARTIFACT_PATHS``).
    """
    return {
        "frps.toml": render_frps_toml(config),
        "nftables.conf": render_nftables_conf(config),
        "port80.Caddyfile": render_port_80_redirect_caddyfile(config),
    }
