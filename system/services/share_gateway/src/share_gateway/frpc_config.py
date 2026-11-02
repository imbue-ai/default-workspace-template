"""frpc.toml rendering: one workspace tunnel to ONE relay of its region.

The runner renders (and runs) one of these per relay in the workspace's
assignment -- the multi-relay design tunnels to every relay of the region, so
any relay can serve any visitor. One ``https``-type proxy claims an EXPLICIT
list of ``<label>.<ws-domain>`` hostnames -- one per registered service plus
the dedicated ``auth`` label -- never the wildcard and never the bare domain.
The relay routes by SNI and drops any hostname it was not told about, so a
scanner that learns the bare domain from Certificate Transparency reaches
nothing. The relay only ever sees ciphertext; the per-share relay token rides
in the client metadata map, and the connector's frps plugin validates it on
Login and checks that every claimed domain is a single label under this
share's domain on NewProxy.

A second proxy, ``screen-media`` of type ``tcp``, carries the Screen's WebRTC stream to
visitors the published port cannot reach (the Screen spec, section 15): the relay binds the
share's ``media_port`` and splices each connection to neko's TCP mux at the workspace's
own interface address (``local_address.py`` says why loopback would not do) on the port
below. It is rendered only when the assignment carries a ``media_port`` and the workspace
has an address; the connector accepts exactly that one tcp claim at exactly that port.

frpc logs in with a ``user``, which frps prefixes onto every proxy name (``<user>.<name>``) and
refuses when a proxy of that name is already registered, so the user has to be unique among
the shares tunnelling to one relay: a workspace share logs in as its own first domain label;
an auth sandbox, whose workspace domain every sandbox of its region shares, as the per-sandbox
auth label the connector chose for it (``frpc_login_user``).
"""

# neko's TCP ICE mux inside the workspace (the Screen spec, section 5.1).
SCREEN_MEDIA_LOCAL_PORT = 59000
SCREEN_MEDIA_PROXY_NAME = "screen-media"


def frpc_login_user(workspace_domain: str, auth_label: str, is_domain_shared: bool) -> str:
    """The user frpc logs in as: the share's first domain label, or the auth label on a shared domain."""
    return auth_label if is_domain_shared else workspace_domain.split(".", 1)[0]


def _render_media_proxy(media_port: int | None, local_media_address: str | None, local_media_port: int) -> str:
    if media_port is None or local_media_address is None:
        return ""
    return f"""
[[proxies]]
name = "{SCREEN_MEDIA_PROXY_NAME}"
type = "tcp"
localIP = "{local_media_address}"
localPort = {local_media_port}
remotePort = {media_port}
"""


def render_frpc_toml(
    relay_host: str,
    relay_port: int,
    relay_token: str,
    workspace_domain: str,
    service_labels: list[str],
    auth_label: str,
    local_https_port: int,
    admin_port: int,
    media_port: int | None,
    local_media_address: str | None,
    local_media_port: int,
    is_domain_shared: bool,
) -> str:
    login_user = frpc_login_user(workspace_domain, auth_label, is_domain_shared)
    # Claim exactly the labels the relay should route: every registered
    # service label plus the auth label. Sorted + de-duplicated for a stable,
    # reload-friendly render. The auth label is always claimed (the login
    # callback must be reachable even before any app is granted).
    claimed_labels = sorted({auth_label, *service_labels})
    custom_domains = ", ".join(f'"{label}.{workspace_domain}"' for label in claimed_labels)
    return f"""\
# Rendered by share-gateway -- do not edit; re-rendered on every share change.
serverAddr = "{relay_host}"
serverPort = {relay_port}

# Keep retrying when this relay is down at start: with several relays per
# region, one being unreachable must not kill its tunnel process for good
# (frp's default exits on a failed FIRST login).
loginFailExit = false

# Tight heartbeats bound the window in which a wedged relay still "holds" this
# tunnel (visitors spliced there would hang until eviction).
transport.heartbeatInterval = 10
transport.heartbeatTimeout = 30

# Encrypt the frpc<->frps control channel (the relay token travels over it).
transport.tls.enable = true

# Loopback admin server so `frpc reload` can hot-add a service claimed while
# shared without dropping the control connection (live viewers stay connected).
webServer.addr = "127.0.0.1"
webServer.port = {admin_port}

user = "{login_user}"

[metadatas]
relay_token = "{relay_token}"

[[proxies]]
name = "share"
type = "https"
localIP = "127.0.0.1"
localPort = {local_https_port}
customDomains = [{custom_domains}]
# Prefix each spliced connection with a PROXY protocol v2 header carrying the
# real client address the relay saw, so caddy (whose listener wrapper consumes
# it) can tell a scanner from a visitor instead of seeing every connection as
# frpc on 127.0.0.1.
transport.proxyProtocolVersion = "v2"
{_render_media_proxy(media_port, local_media_address, local_media_port)}"""
