from pathlib import Path

from share_gateway.caddyfile import parse_registered_apps
from share_gateway.caddyfile import render_caddyfile

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.imbueminds.com"

_APPS_TOML = """
[[apps]]
name = "system_interface"
url = "http://localhost:8000"

[[apps]]
name = "terminal"
url = "http://localhost:7681"

[[apps]]
name = "my-app"
url = "http://127.0.0.1:5000"
"""


def _render(apps_toml: str = _APPS_TOML) -> str:
    return render_caddyfile(
        workspace_domain=_DOMAIN,
        apps=parse_registered_apps(apps_toml),
        tls_cert_path=Path("/secrets/cert.pem"),
        tls_key_path=Path("/secrets/key.pem"),
        https_port=8443,
        gateway_port=8791,
    )


def test_parse_registered_apps_skips_malformed_rows() -> None:
    apps = parse_registered_apps(
        """
[[apps]]
name = "good"
url = "http://localhost:5001"

[[apps]]
name = "no-port"
url = "http://localhost"

[[apps]]
url = "http://localhost:5002"
"""
    )
    assert [(a.name, a.backend_host, a.backend_port) for a in apps] == [("good", "localhost", 5001)]
    assert parse_registered_apps("not toml [[") == []


def test_caddyfile_terminates_tls_with_share_cert_and_routes_by_host() -> None:
    rendered = _render()

    assert f"https://{_DOMAIN}:8443, https://*.{_DOMAIN}:8443 {{" in rendered
    assert "tls /secrets/cert.pem /secrets/key.pem" in rendered
    assert f"@shell host {_DOMAIN}" in rendered
    assert "reverse_proxy localhost:8000" in rendered
    assert f"@service_terminal host terminal.{_DOMAIN}" in rendered
    assert "reverse_proxy localhost:7681" in rendered
    assert f"@service_my_app host my-app.{_DOMAIN}" in rendered
    assert "reverse_proxy 127.0.0.1:5000" in rendered


def test_caddyfile_wires_forward_auth_and_loading_fallback() -> None:
    rendered = _render()

    assert "forward_auth 127.0.0.1:8791" in rendered
    assert "uri /_auth/verify" in rendered
    assert "copy_headers X-Share-Filtered-Cookie>Cookie" in rendered
    assert "handle /_auth/*" in rendered
    assert "rewrite * /_auth/loading" in rendered
    assert "auto_https off" in rendered


def test_caddyfile_without_shell_still_renders() -> None:
    rendered = _render("[[apps]]\nname = \"web\"\nurl = \"http://localhost:5000\"\n")

    assert "@shell" not in rendered
    assert f"@service_web host web.{_DOMAIN}" in rendered
