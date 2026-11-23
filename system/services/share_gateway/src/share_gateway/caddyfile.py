"""Caddyfile rendering: the share's TLS terminator + per-service host routing.

Caddy terminates the workspace's real TLS (cert + key from disk; frpc splices
relay bytes into its local HTTPS port), asks the gateway's ``/_auth/verify``
about every request (forward_auth), and routes by Host: each registered
service's unguessable ``<label>.<domain>`` origin to that service's local
backend, the dedicated ``auth-<rand>.<domain>`` label to the gateway's public
``/_auth/*`` surface (the login callback) and the ``/_agent/*`` surface
(visiting agents), and a registered service whose route is not loaded yet to
the gateway's "Starting up..." page. A registered service caddy
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

Every response caddy sends for the share, a proxied service's or its own,
also carries the browser hardening headers (``Strict-Transport-Security``,
``X-Content-Type-Options``) and no ``Server`` header: the relay is SNI
passthrough and never sees a response, so caddy is the one place they can be
set for every service alike.

Caddy also compresses what the apps serve (``render_compression``): a plain
``GET`` of a text type goes out zstd- or gzip-encoded, while a ``HEAD``, a
ranged request, an event stream and any already-compressed type pass through
untouched.
"""

import threading
import tomllib
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from share_gateway.log import log

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
        program: str = "",
        is_critical: bool = False,
        description: str = "",
        api_schema_path: str = "",
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
        # What the app's manifest says it is for, and where on its origin it
        # describes its API (contracts.md section 2); both empty when undeclared.
        self.description = description
        self.api_schema_path = api_schema_path


class RegistryReadError(Exception):
    """apps.toml exists but cannot be read as a list of apps."""


def parse_registered_apps(apps_toml_text: str) -> list[RegisteredApp]:
    """Parse apps.toml rows into backend targets, skipping malformed or unlabeled entries.

    Raises ``RegistryReadError`` when the text is not TOML or its ``apps`` value is not a list of rows.
    """
    try:
        raw = tomllib.loads(apps_toml_text)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryReadError(f"is not valid TOML ({exc})") from exc
    rows = raw.get("apps", [])
    if not isinstance(rows, list):
        raise RegistryReadError(f"has an 'apps' value that is a {type(rows).__name__}, not a list of rows")
    apps: list[RegisteredApp] = []
    for entry in rows:
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
        description = entry.get("description", "")
        api_schema_path = entry.get("api_schema_path", "")
        apps.append(
            RegisteredApp(
                name=name,
                label=label,
                backend_host=parsed.hostname,
                backend_port=parsed.port,
                is_grantable=is_grantable,
                program=program if isinstance(program, str) else "",
                is_critical=entry.get("critical", False) is True,
                description=description if isinstance(description, str) else "",
                api_schema_path=api_schema_path if isinstance(api_schema_path, str) else "",
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


APPS_TOML_PATH = Path("data/.state/apps.toml")


def read_registered_apps(apps_toml_path: Path) -> list[RegisteredApp]:
    """The registry's rows; none while the file does not exist. Raises ``RegistryReadError`` when it cannot be read."""
    if not apps_toml_path.exists():
        return []
    try:
        text = apps_toml_path.read_text()
    except OSError as exc:
        raise RegistryReadError(f"could not be read ({exc})") from exc
    return parse_registered_apps(text)


class RegisteredAppsReader:
    """The registry, read fresh on each call so an app registered while shared is recognized at once.

    A file that cannot be read or parsed keeps the last good read, as the shell's own inventory does: a
    hand-edited or half-written registry must not turn every app into an unknown address. Before any good
    read (the file not written yet, or unreadable since the gateway started) there are no rows to keep.
    """

    def __init__(self, apps_toml_path: Path) -> None:
        self._apps_toml_path = apps_toml_path
        self._last_good_read: list[RegisteredApp] | None = None
        self._is_failure_logged = False
        self._lock = threading.Lock()

    def read(self) -> list[RegisteredApp] | None:
        """The registry's rows, or the last good read when the file cannot be read; None before any good read."""
        try:
            if not self._apps_toml_path.exists():
                raise RegistryReadError("does not exist")
            apps = read_registered_apps(self._apps_toml_path)
        except RegistryReadError as exc:
            with self._lock:
                if not self._is_failure_logged:
                    self._is_failure_logged = True
                    log(f"Keeping the last good read of the app registry: {self._apps_toml_path} {exc}")
                return self._last_good_read
        with self._lock:
            self._last_good_read = apps
            self._is_failure_logged = False
        return apps

    def apps(self) -> list[RegisteredApp]:
        """The registry's rows as ``read`` answers them, and none before any good read."""
        return self.read() or []


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


# One year, the value scanners (and the browser preload lists) expect.
_HSTS_MAX_AGE_SECONDS = 31_536_000


def _render_hardening_headers(indent: str) -> str:
    """The ``header`` block stamping the hardening headers on every response of one route list.

    Rendered once per route list: ``handle_errors`` runs a handler chain of
    its own, so the site-level block never reaches the 502 not-responding page.
    """
    lines = [
        "# Deferred so it runs after the backend's response headers are copied",
        "# in: the backend's Server header is removed rather than re-added, and",
        "# one Strict-Transport-Security value is sent rather than two.",
        "header {",
        f'    Strict-Transport-Security "max-age={_HSTS_MAX_AGE_SECONDS}; includeSubDomains"',
        "    X-Content-Type-Options nosniff",
        "    -Server",
        "    defer",
        "}",
    ]
    return "\n".join(f"{indent}{line}" for line in lines)


# The text types worth compressing. A server-sent event stream (text/event-stream) is not among them, nor is any
# type whose bytes are compressed already (images other than SVG, wasm, archives, media).
_COMPRESSED_CONTENT_TYPES = (
    "text/html",
    "text/css",
    "text/javascript",
    "text/plain",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
)


def render_compression(indent: str) -> str:
    """The ``encode`` directive compressing what the apps serve, none of which compresses its own responses.

    Only a plain ``GET`` of a named text type is compressed. A ``HEAD`` is not, since caddy would answer it with the
    length of an empty compressed body in place of the real one (the Files app reads a file's size from it); nor a
    ``Range`` request, whose ranges count bytes of the uncompressed body; nor a request for an event stream, since
    an encoder holds the response's headers until the first body byte and a quiet stream would open late.
    """
    lines = [
        "# Compress what the apps serve: a phone on a mobile link pays for",
        "# every byte of each page's bundle.",
        "@compressible {",
        "    not method HEAD",
        "    not header Accept text/event-stream",
        "    not header Range *",
        "}",
        "encode @compressible zstd gzip {",
        "    match {",
        *(f"        header Content-Type {content_type}*" for content_type in _COMPRESSED_CONTENT_TYPES),
        "    }",
        "}",
    ]
    return "\n".join(f"{indent}{line}" for line in lines)


def render_caddyfile(
    workspace_domain: str,
    apps: list[RegisteredApp],
    auth_label: str,
    tls_cert_path: Path,
    tls_key_path: Path,
    https_port: int,
    gateway_port: int,
    chrome_origin: str,
    # Where caddy writes its per-request access log, the owner's record of every request the share served.
    access_log_path: Path,
) -> str:
    """Render the full Caddyfile for one shared workspace."""
    gateway_backend = f"127.0.0.1:{gateway_port}"
    frame_ancestors = build_frame_ancestors_policy(workspace_domain, chrome_origin)
    site_hardening_headers = _render_hardening_headers(indent="    ")
    compression = render_compression(indent="    ")
    error_hardening_headers = _render_hardening_headers(indent="        ")

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
        reverse_proxy {app.backend_host}:{app.backend_port} {{
            # A visiting agent's admission token rides the Authorization header
            # for the gateway alone; the service never sees it, as it never
            # sees the session cookie. No app reads an inbound Authorization
            # header over the share, and owner-exec's signature headers are
            # separate and untouched.
            header_up -Authorization
        }}
    }}
"""
        )

    return f"""\
# Rendered by share-gateway -- do not edit; re-rendered on every apps.toml or share change.
{{
    admin localhost:2019
    auto_https off
    https_port {https_port}
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

    # The owner's per-request record: who connected (the visitor's address,
    # from the PROXY protocol header), which origin, which path, the status,
    # the size and the duration. Never a header, a query string or a response
    # header: the session cookie, a visiting agent's admission token and the
    # login callback's handoff token travel in exactly those, and an app's own
    # headers may carry its credentials. Bounded by the rolling settings; the
    # gateway's request counts (request_counts.py) carry identity instead.
    log {{
        output file {access_log_path} {{
            roll_size 8MiB
            roll_keep 4
            roll_keep_for 720h
        }}
        format filter {{
            wrap json
            fields {{
                request>headers delete
                request>tls delete
                resp_headers delete
                request>uri regexp `\\?.*$` ""
            }}
        }}
    }}

    # The gateway's identity header is trustworthy only because a client can
    # never smuggle its own copy past the auth step: strip any inbound
    # X-Imbue-Identity on every request, ahead of every route below (the auth
    # label's and the error route included), so that the only value a request
    # can carry is the one copy_headers injects from the verified /_auth/verify
    # response.
    request_header -X-Imbue-Identity

{compression}

    # Labels are semi-secret (they gate the relay), so never leak one to
    # another site via the Referer of an outbound navigation.
    header Referrer-Policy same-origin

    # Who may embed this workspace in an iframe: its own origin family plus the
    # hosted Imbue Studio chrome (when the share carries one). This is what makes the
    # chrome's cross-origin iframe render at all; a service's own CSP still
    # applies (multiple CSP headers compose by intersection).
    header Content-Security-Policy "frame-ancestors {frame_ancestors}"

{site_hardening_headers}

    # Health probe, reachable at EVERY workspace origin (not just the auth
    # label) and never auth-gated, so the chrome can probe a workspace it has
    # any origin for. The gateway applies CORS for the chrome origin itself.
    handle /_health {{
        reverse_proxy {gateway_backend}
    }}

    # A registered service whose backend caddy cannot reach (a refused
    # connection, a timeout) gets the gateway's not-responding page rather than
    # a bare error status. It is a path of its own, apart from the
    # route-not-loaded fallback below. A backend's own error answer, the
    # parker's 503 included, is a response rather than a caddy error and passes
    # through as it does locally.
    handle_errors 5xx {{
{error_hardening_headers}
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
        # The visiting-agent surface (join, orientation, invites, the grant
        # list): the gateway authenticates each request itself, by session
        # cookie or bearer admission token.
        handle /_agent/* {{
            reverse_proxy {gateway_backend}
        }}
        handle {{
            respond 404
        }}
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
            # Inject the gateway's verified identity onto the onward request: the
            # filtered cookie and the requester's identity record.
            copy_headers X-Share-Filtered-Cookie>Cookie X-Imbue-Identity
        }}

{"".join(service_blocks)}\
        # A registered service with no route above gets the "Starting up..."
        # page (a service registered while shared becomes routable on the next
        # render; until then this keeps the tab alive, for about a minute).
        handle {{
            rewrite * /_auth/loading
            reverse_proxy {gateway_backend}
        }}
    }}
}}
"""
