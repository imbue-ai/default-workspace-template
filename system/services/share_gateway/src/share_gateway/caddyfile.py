"""Caddyfile rendering: the share's TLS terminator + per-service host routing.

Caddy terminates the workspace's real TLS (cert + key from disk; frpc splices
relay bytes into its local HTTPS port), asks the gateway's ``/_auth/verify``
about every request (forward_auth), and routes by Host: each registered
service's unguessable ``<label>.<domain>`` origin to that service's local
backend, the dedicated ``auth-<rand>.<domain>`` label to the gateway's public
``/_auth/*`` surface (the login callback), and unknown-but-plausible service
origins to the gateway's auto-retrying loading page. A registered service caddy
cannot reach gets the gateway's not-responding page instead, which retries for
about a minute and then says the app is not answering (it covers the moment
between an app stopping and the shell parking its port, or between the shell
releasing the port and the app binding it, the stop-when-no-windows spec,
section 5.7, as well as an app that has crashed for good). The bare workspace domain
is intentionally unrouted (no frpc claim), so scanners that learn it from
Certificate Transparency reach nothing.

The certificate covers ``<domain>`` and ``*.<domain>`` only, so service
origins are one label deep on a share (deeper sub-origins are a deferred
follow-up, gated on wildcard-of-wildcard SANs).
"""

import tomllib
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class RegisteredApp:
    """One ``[[apps]]`` row from ``data/.state/apps.toml``."""

    def __init__(
        self,
        name: str,
        label: str,
        backend_host: str,
        backend_port: int,
        is_grantable: bool,
        program: str,
        is_critical: bool,
    ) -> None:
        self.name = name
        self.label = label
        self.backend_host = backend_host
        self.backend_port = backend_port
        # Whether a per-service grant may admit a visitor to this app; an
        # internal app is reachable only through a workspace-level grant.
        self.is_grantable = is_grantable
        self.program = program
        self.is_critical = is_critical


def parse_registered_apps(apps_toml_text: str) -> list[RegisteredApp]:
    """Parse apps.toml rows into backend targets, skipping malformed or unlabeled entries."""
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
        label = entry.get("label")
        # A row without a label predates the random-label scheme; under the
        # hard cutover it is simply not routable until it re-registers.
        if not isinstance(name, str) or not isinstance(url, str) or not isinstance(label, str) or not label:
            continue
        parsed = urlsplit(url)
        if not parsed.hostname or not parsed.port:
            continue
        # A missing key or a literal ``false`` is grantable; any other value reads as internal.
        is_grantable = entry.get("internal", False) is False
        program = entry.get("program")
        apps.append(
            RegisteredApp(
                name=name,
                label=label,
                backend_host=parsed.hostname,
                backend_port=parsed.port,
                is_grantable=is_grantable,
                program=program if isinstance(program, str) else "",
                is_critical=entry.get("critical", False) is True,
            )
        )
    return apps


def is_started_on_demand(app: RegisteredApp, apps: Sequence[RegisteredApp]) -> bool:
    """Whether the workspace shell starts this app again when a request arrives for it while it is down.

    The same rule as the shell's own (``stoppable_program_of`` and ``parking_target_of`` in
    system_interface's shell): a supervised program that is not critical and not shared with a
    critical app, on a loopback address the shell can hold. Anything else stays down until someone
    restarts it.
    """
    if not app.program or app.is_critical:
        return False
    if any(other.is_critical and other.program == app.program for other in apps):
        return False
    return app.backend_host in _LOOPBACK_HOSTS


def build_label_to_name(apps: Sequence[RegisteredApp]) -> dict[str, str]:
    """Map each registered service's origin label back to its service name (grants are keyed by name)."""
    return {app.label: app.name for app in apps}


def build_grantable_service_names(apps: Sequence[RegisteredApp]) -> frozenset[str]:
    """The names of the registered services a per-service grant may admit a visitor to."""
    return frozenset(app.name for app in apps if app.is_grantable)


def read_registered_apps(apps_toml_path: Path) -> list[RegisteredApp]:
    if not apps_toml_path.exists():
        return []
    try:
        text = apps_toml_path.read_text()
    except OSError:
        return []
    return parse_registered_apps(text)


def build_frame_ancestors_policy(workspace_domain: str, chrome_origin: str) -> str:
    """The ``frame-ancestors`` CSP source list for a shared workspace's responses.

    Always allows the workspace's own origin family (``*.<domain>``) so its
    services can embed one another; adds the hosted-chrome origin only when the
    share carries one, so a share created by an older client stays deny-external.
    """
    sources = ["'self'", f"https://*.{workspace_domain}"]
    if chrome_origin:
        sources.append(chrome_origin)
    return " ".join(sources)


def render_caddyfile(
    workspace_domain: str,
    apps: list[RegisteredApp],
    auth_label: str,
    tls_cert_path: Path,
    tls_key_path: Path,
    https_port: int,
    gateway_port: int,
    chrome_origin: str,
) -> str:
    """Render the full Caddyfile for one shared workspace."""
    gateway_backend = f"127.0.0.1:{gateway_port}"
    frame_ancestors = build_frame_ancestors_policy(workspace_domain, chrome_origin)

    # One matcher per registered service, keyed on its unguessable label host.
    # The matcher id is derived from the (unique) service name; the Host it
    # matches is the label. system_interface is just another labeled service.
    service_blocks: list[str] = []
    for app in sorted(apps, key=lambda entry: entry.name):
        matcher = f"service_{app.name.replace('-', '_')}"
        service_blocks.append(
            f"""\
    @{matcher} host {app.label}.{workspace_domain}
    handle @{matcher} {{
        reverse_proxy {app.backend_host}:{app.backend_port}
    }}
"""
        )

    return f"""\
# Rendered by share-gateway -- do not edit; re-rendered on every apps.toml or share change.
{{
    admin localhost:2019
    auto_https off
    https_port {https_port}
    # Caddy's built-in directive order runs forward_auth BEFORE request_header,
    # which would make the identity strip below delete the value forward_auth
    # just injected. Reorder so the strip runs first.
    order request_header before forward_auth
    servers {{
        # h1/h2 only: h3 is UDP, which the SNI-passthrough relay can never
        # carry, so advertising it (Alt-Svc) just makes browsers probe a
        # dead path before falling back.
        protocols h1 h2
        # frpc prefixes each spliced connection with a PROXY protocol v2
        # header carrying the real client address the relay saw; consume it
        # here (before the TLS wrapper -- the header precedes the handshake)
        # so logs and forwarded headers carry the visitor's IP instead of
        # frpc's 127.0.0.1. Only loopback (frpc) may assert one.
        listener_wrappers {{
            proxy_protocol {{
                timeout 5s
                allow 127.0.0.1/32
            }}
            tls
        }}
    }}
}}

# Only the wildcard is served: the bare workspace domain is deliberately
# unrouted (no frpc claim), so the CT-visible cert name reaches nothing.
https://*.{workspace_domain}:{https_port} {{
    tls {tls_cert_path} {tls_key_path}

    # Labels are semi-secret (they gate the relay), so never leak one to
    # another site via the Referer of an outbound navigation.
    header Referrer-Policy same-origin

    # Who may embed this workspace in an iframe: its own origin family plus the
    # hosted Imbue Studio chrome (when the share carries one). This is what makes the
    # chrome's cross-origin iframe render at all; a service's own CSP still
    # applies (multiple CSP headers compose by intersection).
    header Content-Security-Policy "frame-ancestors {frame_ancestors}"

    # Health probe, reachable at EVERY workspace origin (not just the auth
    # label) and never auth-gated, so the chrome can probe a workspace it has
    # any origin for. The gateway applies CORS for the chrome origin itself.
    handle /_health {{
        reverse_proxy {gateway_backend}
    }}

    # A registered service whose backend refuses the connection gets the
    # gateway's not-responding page rather than a bare 502. It is a path of its
    # own, apart from the route-not-loaded fallback below, because only the
    # gateway can tell how long the address has gone unanswered. The parker's
    # own 503 is a backend answer, not a caddy error, and passes through as it
    # does locally.
    handle_errors 502 {{
        rewrite * /_auth/unresponsive
        reverse_proxy {gateway_backend}
    }}

    # The dedicated auth label is the ONE origin exposing the public /_auth/*
    # surface (the login callback that creates the session). Confining it here
    # -- rather than site-wide -- leaves every app label's path space entirely
    # its own. It is reached without a session (the callback is what mints one)
    # and serves nothing else.
    @auth host {auth_label}.{workspace_domain}
    handle @auth {{
        handle /_auth/* {{
            reverse_proxy {gateway_backend}
        }}
        handle {{
            respond 404
        }}
    }}

    handle {{
        # The gateway's identity header is trustworthy only because a client can
        # never smuggle its own copy past the auth step: strip any inbound
        # X-Imbue-Identity before anything downstream sees the request, then let
        # copy_headers below inject the value the verified /_auth/verify
        # response carries. Textual position does not decide when this runs --
        # the global ``order request_header before forward_auth`` does; without
        # it caddy would run the strip after forward_auth and delete the
        # injected value.
        request_header -X-Imbue-Identity
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
            # Inject the gateway's verified identity onto the onward request: the
            # filtered cookie and the requester's identity record.
            copy_headers X-Share-Filtered-Cookie>Cookie X-Imbue-Identity
        }}

{"".join(service_blocks)}\
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
