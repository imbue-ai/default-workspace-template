from pathlib import Path

from share_gateway.caddyfile import build_label_to_name
from share_gateway.caddyfile import parse_registered_apps
from share_gateway.caddyfile import render_caddyfile

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


def _render(apps_toml: str = _APPS_TOML) -> str:
    return render_caddyfile(
        workspace_domain=_DOMAIN,
        apps=parse_registered_apps(apps_toml),
        auth_label=_AUTH_LABEL,
        tls_cert_path=Path("/secrets/cert.pem"),
        tls_key_path=Path("/secrets/key.pem"),
        https_port=8443,
        gateway_port=8791,
    )


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


def test_caddyfile_wires_forward_auth_and_loading_fallback() -> None:
    rendered = _render()

    assert "forward_auth 127.0.0.1:8791" in rendered
    assert "uri /_auth/verify" in rendered
    assert "header_up X-Forwarded-Upgrade {header.Upgrade}" in rendered
    # The auth subrequest must not itself look like a WebSocket upgrade: the
    # gateway's WSGI server 400s WS handshakes, which would deny every WS
    # connection at the auth step.
    assert "header_up -Upgrade" in rendered
    assert "copy_headers X-Share-Filtered-Cookie>Cookie" in rendered
    assert "rewrite * /_auth/loading" in rendered
    assert "auto_https off" in rendered
    # h1/h2 only: h3 is UDP and cannot traverse the SNI-passthrough relay, so
    # it must not be advertised via Alt-Svc.
    assert "protocols h1 h2" in rendered
