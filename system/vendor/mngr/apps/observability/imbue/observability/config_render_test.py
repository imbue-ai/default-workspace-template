import pytest

from imbue.observability.config_render import render_all_instance_artifacts
from imbue.observability.config_render import render_caddyfile
from imbue.observability.config_render import render_nftables_conf
from imbue.observability.config_render import render_openobserve_env
from imbue.observability.primitives import ObservabilityTierName
from imbue.observability.primitives import TelemetryHostname
from imbue.observability.testing import make_instance_config


def test_openobserve_env_configures_single_node_with_r2_data_and_postgres_meta() -> None:
    rendered = render_openobserve_env(make_instance_config())
    assert 'ZO_LOCAL_MODE="true"' in rendered
    assert 'ZO_LOCAL_MODE_STORAGE="s3"' in rendered
    assert 'ZO_META_STORE="postgres"' in rendered
    assert 'ZO_META_POSTGRES_DSN="postgres://user:pw@db.example/openobserve"' in rendered
    assert 'ZO_S3_SERVER_URL="https://account-1.r2.cloudflarestorage.com"' in rendered
    assert 'ZO_S3_BUCKET_NAME="minds-observability-dev"' in rendered


def test_openobserve_env_escapes_quotes_and_backslashes_in_secret_values() -> None:
    # systemd treats " and \ specially inside double-quoted EnvironmentFile
    # values; an unescaped secret would silently corrupt the environment.
    config = make_instance_config(root_user_password='pw-with-"quote"-and-\\slash')
    rendered = render_openobserve_env(config)
    assert 'ZO_ROOT_USER_PASSWORD="pw-with-\\"quote\\"-and-\\\\slash"' in rendered


def test_openobserve_env_binds_loopback_only() -> None:
    # The public surface is caddy's ingest gate; OpenObserve itself (UI, query
    # API, admin) must never listen on a public interface.
    rendered = render_openobserve_env(make_instance_config())
    assert 'ZO_HTTP_ADDR="127.0.0.1"' in rendered
    assert 'ZO_HTTP_PORT="5080"' in rendered


def test_openobserve_env_disables_phone_home_and_shortens_the_wal_window() -> None:
    rendered = render_openobserve_env(make_instance_config())
    assert 'ZO_TELEMETRY="false"' in rendered
    # 60s (down from the 600s default) bounds how much acked data ever lives
    # only on the instance disk -- the durability envelope in the spec.
    assert 'ZO_MAX_FILE_RETENTION_TIME="60"' in rendered


def test_openobserve_env_sets_the_metrics_retention_as_the_instance_default() -> None:
    # OpenObserve maps each OTLP metric to its own stream, so the metrics
    # retention must be the instance-wide default; log streams get per-stream
    # overrides at provisioning time instead.
    rendered = render_openobserve_env(make_instance_config())
    assert 'ZO_COMPACT_DATA_RETENTION_DAYS="760"' in rendered


def test_caddyfile_exposes_only_otlp_routes_and_healthz() -> None:
    rendered = render_caddyfile(make_instance_config())
    assert "/api/default/v1/logs /api/default/v1/metrics /api/default/v1/traces" in rendered
    assert "path /healthz" in rendered
    # Everything else must 404: a fall-through reverse_proxy would expose the
    # UI and query API to the internet.
    assert "respond 404" in rendered
    assert rendered.count("reverse_proxy 127.0.0.1:5080") == 3


def test_caddyfile_rewrites_bare_otlp_paths_onto_the_default_org() -> None:
    # Modal's integration takes a base endpoint URL and appends the standard
    # /v1/* suffixes; the rewrite makes a pathless base URL work.
    rendered = render_caddyfile(make_instance_config())
    assert "path /v1/logs /v1/metrics /v1/traces" in rendered
    assert "rewrite * /api/default{uri}" in rendered


def test_caddyfile_stamps_the_modal_log_stream_on_bare_otlp_paths() -> None:
    # Modal Secret keys must be valid env var names, so the hyphenated
    # stream-name header cannot ride in the workspace's OTEL_HEADER_* secret;
    # the gate stamps it for the (Modal-only) bare-path senders instead. The
    # org-prefixed routes must stay unstamped -- the fleet collectors set
    # their own stream-name there.
    rendered = render_caddyfile(make_instance_config())
    bare_handler = rendered.split("handle @otlp_bare")[1]
    assert 'request_header stream-name "modal_logs"' in bare_handler
    org_handler = rendered.split("handle @otlp ")[1].split("handle @otlp_bare")[0]
    assert "request_header" not in org_handler


def test_caddyfile_terminates_tls_with_the_origin_certificate() -> None:
    rendered = render_caddyfile(make_instance_config())
    assert "tls /etc/caddy/origin.pem /etc/caddy/origin.key" in rendered
    assert "https://telemetry.minds-test.example:443" in rendered
    # No ACME: auto_https would try to mint public certificates for a hostname
    # whose origin only Cloudflare can reach.
    assert "auto_https off" in rendered


def test_nftables_conf_default_denies_and_admits_443_from_cloudflare_only() -> None:
    rendered = render_nftables_conf(make_instance_config())
    assert "policy drop" in rendered
    assert "tcp dport 22 accept" in rendered
    assert "ip saddr @cloudflare_v4 tcp dport 443 accept" in rendered
    assert "ip6 saddr @cloudflare_v6 tcp dport 443 accept" in rendered
    # A bare 443 accept would let anyone reach the origin around the proxy.
    assert "\n        tcp dport 443 accept" not in rendered


def test_all_instance_artifacts_cover_every_deployed_file() -> None:
    artifacts = render_all_instance_artifacts(make_instance_config())
    assert set(artifacts) == {"openobserve.env", "Caddyfile", "nftables.conf", "origin.pem", "origin.key"}
    assert artifacts["origin.pem"] == "CERT-PEM"
    assert artifacts["origin.key"] == "KEY-PEM"


@pytest.mark.parametrize("bad_hostname", ["Telemetry.Minds.com", "telemetry_x.example", "-telemetry.example", ""])
def test_telemetry_hostname_rejects_non_dns_names(bad_hostname: str) -> None:
    with pytest.raises(ValueError):
        TelemetryHostname(bad_hostname)


@pytest.mark.parametrize("bad_tier", ["prod", "dev-josh-1", "ci", "PRODUCTION", ""])
def test_observability_tier_name_rejects_unknown_tiers(bad_tier: str) -> None:
    with pytest.raises(ValueError):
        ObservabilityTierName(bad_tier)


@pytest.mark.parametrize("good_tier", ["production", "staging", "dev"])
def test_observability_tier_name_accepts_the_three_tiers(good_tier: str) -> None:
    assert ObservabilityTierName(good_tier) == good_tier
