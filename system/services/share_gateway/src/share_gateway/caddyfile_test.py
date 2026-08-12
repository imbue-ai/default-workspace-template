from pathlib import Path

from share_gateway.caddyfile import build_frame_ancestors_policy
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
        f"header Content-Security-Policy \"frame-ancestors 'self' https://*.{_DOMAIN} {_CHROME_ORIGIN}\""
        in rendered
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
    forward_auth_idx = rendered.index("forward_auth")
    assert health_idx < forward_auth_idx


def test_build_frame_ancestors_policy_shapes() -> None:
    assert build_frame_ancestors_policy(_DOMAIN, _CHROME_ORIGIN) == (
        f"'self' https://*.{_DOMAIN} {_CHROME_ORIGIN}"
    )
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
    assert "copy_headers X-Share-Filtered-Cookie>Cookie" in rendered
    assert "rewrite * /_auth/loading" in rendered
    assert "auto_https off" in rendered
    # h1/h2 only: h3 is UDP and cannot traverse the SNI-passthrough relay, so
    # it must not be advertised via Alt-Svc.
    assert "protocols h1 h2" in rendered
