from imbue.observability.bugsink_render import render_all_bugsink_artifacts
from imbue.observability.bugsink_render import render_bugsink_caddyfile
from imbue.observability.bugsink_render import render_bugsink_env
from imbue.observability.testing import make_bugsink_instance_config


def test_bugsink_env_carries_every_setting_the_conf_reads() -> None:
    rendered = render_bugsink_env(make_bugsink_instance_config())

    assert 'SECRET_KEY="django-secret-key-1"' in rendered
    assert 'DATABASE_URL="postgres://user:pw@db.example/bugsink"' in rendered
    assert 'CREATE_SUPERUSER="root@example.com:root-password-1"' in rendered
    # BASE_URL is what Django builds the project DSNs from, so it must be the
    # public ingest hostname, never loopback.
    assert 'BASE_URL="https://errors.minds-test.example"' in rendered
    assert 'BEHIND_HTTPS_PROXY="True"' in rendered
    assert 'PHONEHOME="false"' in rendered
    assert 'MAX_EVENT_AGE_DAYS="30"' in rendered


def test_bugsink_env_admits_loopback_hosts_for_the_ssh_tunneled_ui() -> None:
    rendered = render_bugsink_env(make_bugsink_instance_config())

    # Without the loopback entries Django would 400 every request arriving
    # through the operator's SSH tunnel (Host: localhost:<port>).
    assert 'ALLOWED_HOSTS="errors.minds-test.example,localhost,127.0.0.1"' in rendered


def test_bugsink_env_escapes_quotes_and_backslashes_in_secrets() -> None:
    # Both characters systemd treats specially inside double-quoted
    # EnvironmentFile values must round-trip instead of corrupting the file.
    rendered = render_bugsink_env(make_bugsink_instance_config(secret_key='ke"y\\1'))

    assert 'SECRET_KEY="ke\\"y\\\\1"' in rendered


def test_bugsink_caddyfile_exposes_only_the_dsn_ingest_routes() -> None:
    rendered = render_bugsink_caddyfile(make_bugsink_instance_config())

    assert "path /api/*/envelope/ /api/*/store/" in rendered
    # Everything else -- the login page, UI, and canonical REST API -- must
    # not exist publicly.
    assert "respond 404" in rendered
    assert "/accounts/login" not in rendered
    assert "/api/canonical" not in rendered


def test_bugsink_caddyfile_terminates_tls_with_the_origin_material() -> None:
    rendered = render_bugsink_caddyfile(make_bugsink_instance_config())

    assert "https://errors.minds-test.example:443" in rendered
    assert "tls /etc/caddy/origin.pem /etc/caddy/origin.key" in rendered
    assert "reverse_proxy 127.0.0.1:8300" in rendered


def test_bugsink_caddyfile_stamps_the_true_client_ip_for_django() -> None:
    rendered = render_bugsink_caddyfile(make_bugsink_instance_config())

    # The vendored conf reads X-Real-IP behind a proxy; caddy itself only
    # adds X-Forwarded-For, so the gate maps Cloudflare's CF-Connecting-IP.
    assert "header_up X-Real-IP {header.CF-Connecting-IP}" in rendered


def test_bugsink_artifacts_cover_env_gate_firewall_and_tls() -> None:
    artifacts = render_all_bugsink_artifacts(make_bugsink_instance_config(tier="production"))

    assert set(artifacts) == {"bugsink.env", "Caddyfile", "nftables.conf", "origin.pem", "origin.key"}
    assert artifacts["origin.pem"] == "CERT-PEM"
    assert artifacts["origin.key"] == "KEY-PEM"
    # The firewall is the shared Cloudflare-only origin gate.
    assert "tcp dport 443 accept" in artifacts["nftables.conf"]
    assert "errors.minds-test.example" in artifacts["nftables.conf"]
