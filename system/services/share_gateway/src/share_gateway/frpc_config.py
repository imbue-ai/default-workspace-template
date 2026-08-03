"""frpc.toml rendering: the workspace's outbound tunnel to its region's relay.

One ``https``-type proxy claims an EXPLICIT list of ``<label>.<ws-domain>``
hostnames -- one per registered service plus the dedicated ``auth`` label --
never the wildcard and never the bare domain. The relay routes by SNI and
drops any hostname it was not told about, so a scanner that learns the bare
domain from Certificate Transparency reaches nothing. The relay only ever sees
ciphertext; the per-share relay token rides in the client metadata map, and
the connector's frps plugin validates it on Login and checks that every
claimed domain is a single label under this share's domain on NewProxy.
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
) -> str:
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

# Encrypt the frpc<->frps control channel (the relay token travels over it).
transport.tls.enable = true

# Loopback admin server so `frpc reload` can hot-add a service claimed while
# shared without dropping the control connection (live viewers stay connected).
webServer.addr = "127.0.0.1"
webServer.port = {admin_port}

user = "{workspace_domain.split(".", 1)[0]}"

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
"""
