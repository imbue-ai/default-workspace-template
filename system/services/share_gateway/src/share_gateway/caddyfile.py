"""Caddyfile rendering: the share's TLS terminator + per-service host routing.

Caddy terminates the workspace's real TLS (cert + key from disk; frpc splices
relay bytes into its local HTTPS port), asks the gateway's ``/_auth/verify``
about every request (forward_auth), and routes by Host: the bare workspace
domain to the shell (``system_interface``), ``<name>.<domain>`` to that
registered service's local backend, and unknown-but-plausible service origins
to the gateway's auto-retrying loading page.

The certificate covers ``<domain>`` and ``*.<domain>`` only, so service
origins are one label deep on a share (deeper sub-origins are a deferred
follow-up, gated on wildcard-of-wildcard SANs).
"""

import tomllib
from pathlib import Path
from urllib.parse import urlsplit

_SHELL_SERVICE_NAME = "system_interface"


class RegisteredApp:
    """One ``[[apps]]`` row from ``data/.state/apps.toml``."""

    def __init__(self, name: str, backend_host: str, backend_port: int) -> None:
        self.name = name
        self.backend_host = backend_host
        self.backend_port = backend_port


def parse_registered_apps(apps_toml_text: str) -> list[RegisteredApp]:
    """Parse apps.toml rows into backend targets, skipping malformed entries."""
    try:
        raw = tomllib.loads(apps_toml_text)
    except tomllib.TOMLDecodeError:
        return []
    apps: list[RegisteredApp] = []
    for entry in raw.get("apps", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        url = entry.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        if not parsed.hostname or not parsed.port:
            continue
        apps.append(RegisteredApp(name=name, backend_host=parsed.hostname, backend_port=parsed.port))
    return apps


def read_registered_apps(apps_toml_path: Path) -> list[RegisteredApp]:
    if not apps_toml_path.exists():
        return []
    try:
        text = apps_toml_path.read_text()
    except OSError:
        return []
    return parse_registered_apps(text)


def render_caddyfile(
    workspace_domain: str,
    apps: list[RegisteredApp],
    tls_cert_path: Path,
    tls_key_path: Path,
    https_port: int,
    gateway_port: int,
) -> str:
    """Render the full Caddyfile for one shared workspace."""
    gateway_backend = f"127.0.0.1:{gateway_port}"

    service_blocks: list[str] = []
    shell_backend = None
    for app in sorted(apps, key=lambda entry: entry.name):
        if app.name == _SHELL_SERVICE_NAME:
            shell_backend = f"{app.backend_host}:{app.backend_port}"
            continue
        service_blocks.append(
            f"""\
    @service_{app.name.replace("-", "_")} host {app.name}.{workspace_domain}
    handle @service_{app.name.replace("-", "_")} {{
        reverse_proxy {app.backend_host}:{app.backend_port}
    }}
"""
        )

    shell_block = (
        f"""\
    @shell host {workspace_domain}
    handle @shell {{
        reverse_proxy {shell_backend}
    }}
"""
        if shell_backend is not None
        else ""
    )

    return f"""\
# Rendered by share-gateway -- do not edit; re-rendered on every apps.toml or share change.
{{
    admin localhost:2019
    auto_https off
    https_port {https_port}
}}

https://{workspace_domain}:{https_port}, https://*.{workspace_domain}:{https_port} {{
    tls {tls_cert_path} {tls_key_path}

    # The gateway's own endpoints (login callback, loading page) are reachable
    # without a session -- the callback is what creates the session.
    handle /_auth/* {{
        reverse_proxy {gateway_backend}
    }}

    handle {{
        forward_auth {gateway_backend} {{
            uri /_auth/verify
            # forward_auth copies the original request's headers into the auth
            # subrequest, Connection/Upgrade included, which makes caddy treat
            # the subrequest itself as a WebSocket upgrade -- and the gateway's
            # WSGI server rejects WebSocket handshakes with a 400, killing
            # every WS connection at the auth step. Capture the upgrade marker
            # for the gateway's Origin rule first, then strip Upgrade so the
            # subrequest stays a plain GET. Origin and Cookie are end-to-end
            # and forward on their own.
            header_up X-Forwarded-Upgrade {{header.Upgrade}}
            header_up -Upgrade
            copy_headers X-Share-Filtered-Cookie>Cookie
        }}

{shell_block}{"".join(service_blocks)}\
        # Unknown-but-plausible service origins get the auto-retrying loading
        # page (a service registered while shared becomes routable on the next
        # render; until then this keeps the tab alive).
        handle {{
            rewrite * /_auth/loading
            reverse_proxy {gateway_backend}
        }}
    }}
}}
"""
