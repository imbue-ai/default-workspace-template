import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from share_gateway.caddyfile import RegisteredApp
from share_gateway.caddyfile import build_frame_ancestors_policy
from share_gateway.caddyfile import build_grantable_service_names
from share_gateway.caddyfile import build_internal_app_names
from share_gateway.caddyfile import build_label_to_name
from share_gateway.caddyfile import parse_registered_apps
from share_gateway.caddyfile import render_caddyfile
from share_gateway.caddyfile import render_compression
from share_gateway.caddyfile import render_web_client_access

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"
_AUTH_LABEL = "auth-x7k9q2w1"

_APPS_TOML = """
[[apps]]
name = "system_interface"
url = "http://localhost:8000"
label = "system_interface-aaaa1111"

[[apps]]
name = "terminal"
url = "http://localhost:7681"
label = "terminal-bbbb2222"

[[apps]]
name = "my-app"
url = "http://127.0.0.1:5000"
label = "my-app-cccc3333"
"""


_CHROME_ORIGIN = "https://minds.imbue.com"


def _render(apps_toml: str = _APPS_TOML, chrome_origin: str = _CHROME_ORIGIN) -> str:
    return render_caddyfile(
        workspace_domain=_DOMAIN,
        apps=parse_registered_apps(apps_toml),
        auth_label=_AUTH_LABEL,
        tls_cert_path=Path("/secrets/cert.pem"),
        tls_key_path=Path("/secrets/key.pem"),
        https_port=8443,
        gateway_port=8791,
        chrome_origin=chrome_origin,
        access_log_path=Path("/state/share_gateway/access.log"),
    )


def test_caddyfile_consumes_proxy_protocol_from_loopback_before_tls() -> None:
    # frpc stamps each spliced connection with PROXY protocol v2; the wrapper
    # must sit before the tls wrapper (the header precedes the handshake) and
    # only loopback -- frpc -- may assert a client address.
    rendered = _render()

    assert "listener_wrappers" in rendered
    assert "proxy_protocol" in rendered
    assert "allow 127.0.0.1/32" in rendered
    proxy_protocol_idx = rendered.index("proxy_protocol")
    tls_wrapper_idx = rendered.index("tls", proxy_protocol_idx)
    assert proxy_protocol_idx < tls_wrapper_idx


def test_parse_registered_apps_skips_malformed_or_unlabeled_rows() -> None:
    apps = parse_registered_apps(
        """
[[apps]]
name = "good"
url = "http://localhost:5001"
label = "good-12345678"

[[apps]]
name = "no-port"
url = "http://localhost"
label = "no-port-00000000"

[[apps]]
name = "no-label"
url = "http://localhost:5003"

[[apps]]
url = "http://localhost:5002"
label = "orphan-99999999"
"""
    )
    assert [(a.name, a.label, a.backend_host, a.backend_port) for a in apps] == [
        ("good", "good-12345678", "localhost", 5001)
    ]
    assert parse_registered_apps("not toml [[") == []


def test_parse_registered_apps_carries_the_description_and_the_api_schema_path() -> None:
    (described, bare) = parse_registered_apps(
        """
[[apps]]
name = "notes"
url = "http://localhost:5001"
label = "notes-11111111"
description = "Keeps the person's notes."
api_schema_path = "/api/schema"

[[apps]]
name = "ttyd"
url = "http://localhost:5002"
label = "ttyd-22222222"
description = 7
"""
    )
    assert (described.description, described.api_schema_path) == ("Keeps the person's notes.", "/api/schema")
    assert (bare.description, bare.api_schema_path) == ("", "")


def test_every_registered_row_but_an_internal_one_is_grantable() -> None:
    apps = parse_registered_apps(
        """
[[apps]]
name = "notes"
url = "http://localhost:5001"
label = "notes-11111111"

[[apps]]
name = "chat"
url = "http://localhost:5002"
label = "chat-22222222"
system = true

[[apps]]
name = "terminal-pty"
url = "http://localhost:5003"
label = "terminal-pty-33333333"
internal = true

[[apps]]
name = "odd"
url = "http://localhost:5004"
label = "odd-44444444"
internal = "no"
"""
    )
    assert build_grantable_service_names(apps) == frozenset({"notes", "chat"})


def test_build_label_to_name_maps_labels_back_to_names() -> None:
    apps = parse_registered_apps(_APPS_TOML)
    assert build_label_to_name(apps) == {
        "system_interface-aaaa1111": "system_interface",
        "terminal-bbbb2222": "terminal",
        "my-app-cccc3333": "my-app",
    }


def test_caddyfile_routes_each_service_by_its_label_host() -> None:
    rendered = _render()

    # Only the wildcard is served; the bare domain is deliberately unrouted.
    assert f"https://*.{_DOMAIN}:8443 {{" in rendered
    assert f"https://{_DOMAIN}:8443" not in rendered
    assert "tls /secrets/cert.pem /secrets/key.pem" in rendered
    # The shell is just another labeled service now (no @shell special case).
    assert "@shell" not in rendered
    assert f"@service_system_interface host system_interface-aaaa1111.{_DOMAIN}" in rendered
    assert "reverse_proxy localhost:8000" in rendered
    assert f"@service_terminal host terminal-bbbb2222.{_DOMAIN}" in rendered
    assert "reverse_proxy localhost:7681" in rendered
    assert f"@service_my_app host my-app-cccc3333.{_DOMAIN}" in rendered
    assert "reverse_proxy 127.0.0.1:5000" in rendered
    # A service's readable name must not be routable on its own.
    assert f"host terminal.{_DOMAIN}" not in rendered


def test_caddyfile_confines_auth_surface_to_the_dedicated_auth_label() -> None:
    rendered = _render()

    assert f"@auth host {_AUTH_LABEL}.{_DOMAIN}" in rendered
    assert "handle /_auth/*" in rendered
    # Referrer-Policy keeps the semi-secret labels from leaking via Referer.
    assert "header Referrer-Policy same-origin" in rendered


def test_caddyfile_appends_frame_ancestors_allowing_own_family_and_chrome() -> None:
    rendered = _render()

    assert (
        f"header Content-Security-Policy \"frame-ancestors 'self' https://*.{_DOMAIN} {_CHROME_ORIGIN}\"" in rendered
    )


def test_caddyfile_frame_ancestors_omits_chrome_when_share_carries_none() -> None:
    rendered = _render(chrome_origin="")

    assert f"header Content-Security-Policy \"frame-ancestors 'self' https://*.{_DOMAIN}\"" in rendered
    assert "minds.imbue.com" not in rendered


def test_caddyfile_routes_health_site_wide_and_unauthenticated() -> None:
    rendered = _render()

    # /_health is reachable at every origin (a plain handle, not under @auth or
    # behind forward_auth) so the chrome can probe any workspace origin it has.
    assert "handle /_health {" in rendered
    health_idx = rendered.index("handle /_health {")
    forward_auth_idx = rendered.index("forward_auth 127.0.0.1:8791")
    assert health_idx < forward_auth_idx


def test_build_frame_ancestors_policy_shapes() -> None:
    assert build_frame_ancestors_policy(_DOMAIN, _CHROME_ORIGIN) == (f"'self' https://*.{_DOMAIN} {_CHROME_ORIGIN}")
    assert build_frame_ancestors_policy(_DOMAIN, "") == f"'self' https://*.{_DOMAIN}"


def test_caddyfile_wires_forward_auth_and_loading_fallback() -> None:
    rendered = _render()

    assert "forward_auth 127.0.0.1:8791" in rendered
    assert "uri /_auth/verify" in rendered
    assert "header_up X-Forwarded-Upgrade {header.Upgrade}" in rendered
    # The auth subrequest must not itself look like a WebSocket upgrade: the
    # gateway's WSGI server 400s WS handshakes, which would deny every WS
    # connection at the auth step.
    assert "header_up -Upgrade" in rendered
    # Inbound copies of the gateway's identity headers are stripped before the
    # request reaches a backend, and only the verified /_auth/verify values are
    # injected -- so a client can never forge ownership or an email.
    assert "request_header -X-Imbue-Identity" in rendered
    assert "X-Share-Owner" not in rendered
    assert "copy_headers X-Share-Filtered-Cookie>Cookie X-Imbue-Identity" in rendered
    # Textual position inside a handle block does NOT decide when the strip
    # runs: caddy sorts directives by its own order, which puts forward_auth
    # before request_header. Only the global order override makes the strip
    # run first.
    assert "order request_header before forward_auth" in rendered
    assert "rewrite * /_auth/loading" in rendered
    assert "auto_https off" in rendered
    # h1/h2 only: h3 is UDP and cannot traverse the SNI-passthrough relay, so
    # it must not be advertised via Alt-Svc.
    assert "protocols h1 h2" in rendered


def test_caddyfile_serves_the_loading_page_for_a_backend_it_cannot_reach() -> None:
    rendered = _render()

    assert "handle_errors 502 {" in rendered
    assert rendered.count("rewrite * /_auth/loading") == 2


def test_render_caddyfile_routes_the_agent_surface_and_strips_the_admission_token() -> None:
    rendered = render_caddyfile(
        workspace_domain="alpha.example.com",
        apps=[RegisteredApp("chat", "chat-ch111111", "127.0.0.1", 8010, is_grantable=True)],
        auth_label="auth-x7k9q2w1",
        tls_cert_path=Path("/tmp/cert.pem"),
        tls_key_path=Path("/tmp/key.pem"),
        https_port=8443,
        gateway_port=8791,
        chrome_origin="",
        access_log_path=Path("/tmp/access.log"),
    )
    auth_block = rendered[rendered.index("@auth host auth-x7k9q2w1.alpha.example.com") :]
    assert "handle /_agent/* {" in auth_block.split("handle {")[0]
    service_block = rendered[rendered.index("@service_chat host") :]
    assert "header_up -Authorization" in service_block.split("handle {")[0]


def _hardening_block(indent: str) -> str:
    return "\n".join(
        f"{indent}{line}"
        for line in (
            "header {",
            '    Strict-Transport-Security "max-age=31536000; includeSubDomains"',
            "    X-Content-Type-Options nosniff",
            "    -Server",
            "    defer",
            "}",
        )
    )


def test_caddyfile_hardens_every_response_including_the_error_path() -> None:
    rendered = _render()

    assert _hardening_block("    ") in rendered
    # handle_errors runs a handler chain of its own, so the 502 loading page
    # needs the block of its own.
    error_block_start = rendered.index("handle_errors 502 {")
    error_block_end = rendered.index("rewrite * /_auth/loading", error_block_start)
    assert _hardening_block("        ") in rendered[error_block_start:error_block_end]
    # The appended frame-ancestors policy stays a plain (non-deferred) add: a
    # deferred set would overwrite a service's own CSP instead of composing.
    assert rendered.count("Content-Security-Policy") == 1


def test_caddyfile_writes_a_bounded_access_log_without_headers_or_query_strings() -> None:
    rendered = _render()

    log_block = rendered[rendered.index("    log {") : rendered.index("    # Labels are semi-secret")]
    assert "output file /state/share_gateway/access.log {" in log_block
    assert "roll_size 8MiB" in log_block and "roll_keep 4" in log_block and "roll_keep_for 720h" in log_block
    assert "wrap json" in log_block
    assert "request>headers delete" in log_block
    assert "resp_headers delete" in log_block
    assert 'request>uri regexp `\\?.*$` ""' in log_block
    # The log sits inside the site block, so it covers every origin of the share.
    assert rendered.index("https://*.") < rendered.index("    log {")


def test_caddyfile_compresses_every_origin() -> None:
    rendered = _render()

    assert render_compression(indent="    ") in rendered
    # In the site block, ahead of every route, so it covers each app's origin.
    assert rendered.index("https://*.") < rendered.index("    encode @compressible") < rendered.index("handle @auth")


def _caddy_binary() -> str | None:
    configured = os.environ.get("CADDY_BINARY", "")
    if configured and Path(configured).exists():
        return configured
    return shutil.which("caddy")


def test_the_rendered_caddyfile_adapts_with_the_access_log_filters_in_place(tmp_path: Path) -> None:
    """The log block's syntax is caddy's to judge: ``caddy adapt`` parses the rendered file exactly as the gateway
    would start it. Skipped where no caddy is installed; the workspace image and CI's both install the pinned one."""
    caddy = _caddy_binary()
    if caddy is None:
        pytest.skip("no caddy binary on PATH or in CADDY_BINARY")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(_render())

    adapted = subprocess.run(
        [caddy, "adapt", "--config", str(caddyfile), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert adapted.returncode == 0, adapted.stderr
    logs = json.loads(adapted.stdout)["logging"]["logs"]
    (access_log,) = [log for name, log in logs.items() if name != "default"]
    assert access_log["writer"] == {
        "filename": "/state/share_gateway/access.log",
        "output": "file",
        "roll_keep": 4,
        "roll_keep_days": 30,
        "roll_size_mb": 8,
    }
    assert access_log["encoder"]["format"] == "filter" and access_log["encoder"]["wrap"] == {"format": "json"}
    (server,) = json.loads(adapted.stdout)["apps"]["http"]["servers"].values()
    encoders = [
        handler for route in server["routes"] for handler in _handlers_of(route) if handler["handler"] == "encode"
    ]
    assert encoders and set(encoders[0]["encodings"]) == {"gzip", "zstd"}
    assert access_log["encoder"]["fields"] == {
        "request>headers": {"filter": "delete"},
        "request>tls": {"filter": "delete"},
        "resp_headers": {"filter": "delete"},
        "request>uri": {"filter": "regexp", "regexp": "\\?.*$"},
    }


def _handlers_of(route: dict) -> list[dict]:
    """Every handler under one adapted route, through the subroutes caddy nests a site block's directives in."""
    found: list[dict] = []
    for handler in route.get("handle", []):
        found.append(handler)
        for nested in handler.get("routes", []):
            found.extend(_handlers_of(nested))
    return found


_APP_SCRIPT = b"function f() { return 'abcdefghij'; }\n" * 2000
# The test app's event stream stays quiet until the test has its headers, then sends its one event.
_STREAM_RELEASED = threading.Event()


class _AppBackend(BaseHTTPRequestHandler):
    """An app behind the share: a script it serves whole, in a range or as its headers alone, and a quiet event
    stream."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self._serve(is_head=True)

    def do_GET(self) -> None:
        self._serve(is_head=False)

    def _serve(self, is_head: bool) -> None:
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            _STREAM_RELEASED.wait(timeout=10)
            event = b"data: hello\n\n"
            self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(event), event))
            return
        byte_range = self.headers.get("Range")
        if byte_range is None:
            body = _APP_SCRIPT
            self.send_response(200)
        else:
            first, last = (int(bound) for bound in byte_range.removeprefix("bytes=").split("-"))
            body = _APP_SCRIPT[first : last + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {first}-{last}/{len(_APP_SCRIPT)}")
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not is_head:
            self.wfile.write(body)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _request(
    port: int, method: str, path: str, headers: dict[str, str], timeout_seconds: float = 10
) -> http.client.HTTPResponse:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_seconds)
    connection.request(method, path, headers=headers)
    return connection.getresponse()


@contextmanager
def _caddy_running(caddy: str, caddyfile_text: str, tmp_path: Path) -> Generator[None, None, None]:
    """Run caddy on ``caddyfile_text`` for as long as the block lasts, entered once caddy is serving it."""
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(caddyfile_text)
    caddy_env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    # Caddy connects back to this listener once its config is running, so no request races its start.
    ready = socket.create_server(("127.0.0.1", 0))
    ready.settimeout(15)
    with (tmp_path / "caddy.log").open("w") as caddy_log:
        server = subprocess.Popen(
            [
                caddy,
                "run",
                "--config",
                str(caddyfile),
                "--adapter",
                "caddyfile",
                "--pingback",
                f"127.0.0.1:{ready.getsockname()[1]}",
            ],
            stdout=caddy_log,
            stderr=subprocess.STDOUT,
            env=caddy_env,
        )
    try:
        try:
            ready.accept()[0].close()
        except TimeoutError:
            pytest.fail((tmp_path / "caddy.log").read_text())
        yield
    finally:
        ready.close()
        server.terminate()
        server.wait(timeout=10)


def test_caddy_compresses_a_plain_get_but_not_a_head_a_range_or_an_event_stream(tmp_path: Path) -> None:
    """The rendered compression, run by caddy in front of an app: a plain GET is compressed, while a HEAD keeps the
    real length, a ranged request gets a plain 206, and an event stream's headers arrive while it is still quiet,
    before its first event. Skipped where no caddy is installed, as the adapt test is."""
    caddy = _caddy_binary()
    if caddy is None:
        pytest.skip("no caddy binary on PATH or in CADDY_BINARY")
    backend = ThreadingHTTPServer(("127.0.0.1", 0), _AppBackend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    port = _free_port()
    caddyfile_text = (
        "{\n    admin off\n    auto_https off\n}\n"
        f"http://127.0.0.1:{port} {{\n{render_compression(indent='    ')}\n"
        f"    reverse_proxy 127.0.0.1:{backend.server_port}\n}}\n"
    )
    _STREAM_RELEASED.clear()
    try:
        with _caddy_running(caddy, caddyfile_text, tmp_path):
            whole = _request(port, "GET", "/app.js", {"Accept-Encoding": "gzip"})
            assert whole.getheader("Content-Encoding") == "gzip"
            assert len(whole.read()) < len(_APP_SCRIPT)

            head = _request(port, "HEAD", "/app.js", {"Accept-Encoding": "gzip"})
            assert head.getheader("Content-Encoding") is None
            assert head.getheader("Content-Length") == str(len(_APP_SCRIPT))

            # Well past the size caddy starts compressing at, so only the matcher keeps the range plain.
            ranged = _request(port, "GET", "/app.js", {"Accept-Encoding": "gzip", "Range": "bytes=0-9999"})
            assert ranged.status == 206
            assert ranged.getheader("Content-Encoding") is None
            assert ranged.read() == _APP_SCRIPT[:10000]

            # The headers arrive while the stream has sent nothing yet: an encoder holding them would time this
            # out, well before the app gives up waiting and sends its event anyway.
            stream = _request(
                port, "GET", "/events", {"Accept-Encoding": "gzip", "Accept": "text/event-stream"}, timeout_seconds=5
            )
            assert stream.getheader("Content-Encoding") is None
            _STREAM_RELEASED.set()
            assert stream.read() == b"data: hello\n\n"
    finally:
        _STREAM_RELEASED.set()
        backend.shutdown()


_APPS_TOML_WITH_AN_INTERNAL_APP = (
    _APPS_TOML
    + """
[[apps]]
name = "system_interface"
url = "http://localhost:8000"
label = "shell-dddd4444"
internal = true
"""
)
_INTERNAL_HOST = f"shell-dddd4444.{_DOMAIN}"
_GRANTABLE_HOST = f"my-app-cccc3333.{_DOMAIN}"
_AUTH_HOST = f"{_AUTH_LABEL}.{_DOMAIN}"


def test_caddyfile_lets_the_web_client_call_the_workspace_routes_of_internal_apps_alone() -> None:
    rendered = _render(apps_toml=_APPS_TOML_WITH_AN_INTERNAL_APP)

    matcher = rendered.split("@web_client_workspace_routes {", 1)[1].split("}", 1)[0]
    assert f"host {_INTERNAL_HOST}\n" in matcher
    assert "path /api/workspace/*" in matcher
    assert f"header Origin {_CHROME_ORIGIN}" in matcher
    # An app a per-app grant admits a visitor to is never one of the hosts.
    assert _GRANTABLE_HOST not in matcher
    assert f'Access-Control-Allow-Origin "{_CHROME_ORIGIN}"' in rendered
    assert 'Access-Control-Allow-Credentials "true"' in rendered
    # The permission check is answered ahead of the auth label's routes and of the auth step.
    assert rendered.index("handle @web_client_workspace_routes_permission_check") < rendered.index("handle @auth")
    assert rendered.index("handle @web_client_agent_routes_permission_check") < rendered.index("handle @auth")
    assert rendered.index("handle @web_client_workspace_routes_permission_check") < rendered.index(
        "forward_auth 127.0.0.1"
    )


def test_caddyfile_lets_the_web_client_call_the_agent_routes_on_the_auth_label() -> None:
    rendered = _render()

    matcher = rendered.split("@web_client_agent_routes {", 1)[1].split("}", 1)[0]
    assert f"host {_AUTH_HOST}\n" in matcher
    assert "path /_agent/*" in matcher
    # With no internal app registered there is no workspace route to call.
    assert "@web_client_workspace_routes" not in rendered


def test_caddyfile_names_no_other_origin_when_the_share_carries_no_web_client() -> None:
    rendered = _render(apps_toml=_APPS_TOML_WITH_AN_INTERNAL_APP, chrome_origin="")

    assert "Access-Control-Allow-Origin" not in rendered
    assert "@web_client" not in rendered


def test_only_an_internal_row_is_an_internal_app() -> None:
    apps = parse_registered_apps(_APPS_TOML_WITH_AN_INTERNAL_APP)

    assert build_internal_app_names(apps) == frozenset({"system_interface"})


class _RecordingBackend(BaseHTTPRequestHandler):
    """An app behind the share that answers every request and records which ones reached it."""

    protocol_version = "HTTP/1.1"
    reached: list[tuple[str, str]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def _answer(self) -> None:
        type(self).reached.append((self.command, self.path))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_PUT = _answer
    do_OPTIONS = _answer


def test_caddy_answers_the_web_clients_permission_check_and_lets_it_read_answers(tmp_path: Path) -> None:
    """The rendered web client access, run by caddy in front of an app: the browser's permission check is answered
    by caddy and never reaches the app, an answer to the web client's origin carries the headers that let its page
    read it, and no other origin, path or host gets either. Skipped where no caddy is installed."""
    caddy = _caddy_binary()
    if caddy is None:
        pytest.skip("no caddy binary on PATH or in CADDY_BINARY")
    _RecordingBackend.reached = []
    backend = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingBackend)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    port = _free_port()
    access = render_web_client_access(
        _DOMAIN, parse_registered_apps(_APPS_TOML_WITH_AN_INTERNAL_APP), _AUTH_LABEL, _CHROME_ORIGIN
    )
    caddyfile_text = (
        "{\n    admin off\n    auto_https off\n}\n"
        f"http://:{port} {{\n{access}\n"
        f"    handle {{\n        reverse_proxy 127.0.0.1:{backend.server_port}\n    }}\n}}\n"
    )

    def ask(method: str, host: str, path: str, origin: str) -> http.client.HTTPResponse:
        response = _request(port, method, path, {"Host": host, "Origin": origin})
        response.read()
        return response

    try:
        with _caddy_running(caddy, caddyfile_text, tmp_path):
            check = ask("OPTIONS", _INTERNAL_HOST, "/api/workspace/update/schedule", _CHROME_ORIGIN)
            assert check.status == 204
            assert check.getheader("Access-Control-Allow-Origin") == _CHROME_ORIGIN
            assert check.getheader("Access-Control-Allow-Credentials") == "true"
            assert check.getheader("Access-Control-Allow-Methods") == "GET, POST, PUT, PATCH, DELETE"
            assert check.getheader("Access-Control-Allow-Headers") == "Content-Type"
            assert _RecordingBackend.reached == []

            call = ask("PUT", _INTERNAL_HOST, "/api/workspace/profile", _CHROME_ORIGIN)
            assert call.status == 200
            assert call.getheader("Access-Control-Allow-Origin") == _CHROME_ORIGIN
            assert call.getheader("Access-Control-Allow-Credentials") == "true"
            assert {value.strip() for value in (call.getheader("Vary") or "").split(",")} == {
                "Accept-Encoding",
                "Origin",
            }

            agent_check = ask("OPTIONS", _AUTH_HOST, "/_agent/grants/agent-1", _CHROME_ORIGIN)
            assert agent_check.status == 204
            agent_call = ask("GET", _AUTH_HOST, "/_agent/grants", _CHROME_ORIGIN)
            assert agent_call.getheader("Access-Control-Allow-Origin") == _CHROME_ORIGIN
            assert _RecordingBackend.reached == [("PUT", "/api/workspace/profile"), ("GET", "/_agent/grants")]

            # Another origin, another path, and an app a per-app grant admits to: each reaches the app as it always
            # did, permission check included, and its answer names no origin.
            _RecordingBackend.reached = []
            unanswered = (
                ("OPTIONS", _INTERNAL_HOST, "/api/workspace/profile", "https://evil.example.com"),
                ("PUT", _INTERNAL_HOST, "/api/workspace/profile", "https://evil.example.com"),
                ("OPTIONS", _INTERNAL_HOST, "/api/chats", _CHROME_ORIGIN),
                ("PUT", _INTERNAL_HOST, "/api/chats", _CHROME_ORIGIN),
                ("OPTIONS", _GRANTABLE_HOST, "/api/workspace/profile", _CHROME_ORIGIN),
                ("PUT", _GRANTABLE_HOST, "/api/workspace/profile", _CHROME_ORIGIN),
                ("OPTIONS", _INTERNAL_HOST, "/_agent/grants", _CHROME_ORIGIN),
            )
            for method, host, path, origin in unanswered:
                other = ask(method, host, path, origin)
                assert other.status == 200, (method, host, path, origin)
                assert other.getheader("Access-Control-Allow-Origin") is None, (method, host, path, origin)
            assert _RecordingBackend.reached == [(method, path) for method, _host, path, _origin in unanswered]
    finally:
        backend.shutdown()
