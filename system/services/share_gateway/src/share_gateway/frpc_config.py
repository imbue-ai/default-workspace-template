"""frpc.toml rendering: the workspace's outbound tunnel to its region's relay.

One ``https``-type proxy claims the workspace's bare domain plus its wildcard;
the relay routes by SNI and splices raw TLS bytes into caddy's local HTTPS
port, so the relay never sees plaintext. The per-share relay token rides in
the client metadata map -- the connector's frps plugin validates it on Login
and checks the claimed domains on NewProxy.
"""


def render_frpc_toml(
    relay_host: str,
    relay_port: int,
    relay_token: str,
    workspace_domain: str,
    local_https_port: int,
) -> str:
    return f"""\
# Rendered by share-gateway -- do not edit; re-rendered on every share change.
serverAddr = "{relay_host}"
serverPort = {relay_port}

# Encrypt the frpc<->frps control channel (the relay token travels over it).
transport.tls.enable = true

user = "{workspace_domain.split(".", 1)[0]}"

[metadatas]
relay_token = "{relay_token}"

[[proxies]]
name = "share"
type = "https"
localIP = "127.0.0.1"
localPort = {local_https_port}
customDomains = ["{workspace_domain}", "*.{workspace_domain}"]
"""
