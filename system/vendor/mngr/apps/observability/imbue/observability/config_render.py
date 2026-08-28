"""Pure renderers for an observability instance host's on-disk config.

The instance is one OpenObserve process behind a caddy ingest gate: caddy
terminates TLS with the tier's Cloudflare origin certificate and exposes ONLY
the OTLP ingest routes plus /healthz, so the UI and query API never exist
publicly (operators reach them over an SSH tunnel to loopback). The renderers
are pure so the deploy CLI can preview them and so they are unit-testable
without touching a host.
"""

from typing import Final

from imbue.imbue_common.pure import pure
from imbue.observability.data_types import ObservabilityInstanceConfig
from imbue.observability.primitives import MODAL_LOG_STREAM_NAME
from imbue.observability.primitives import OPENOBSERVE_HTTP_PORT
from imbue.observability.primitives import OPENOBSERVE_ORGANIZATION

# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/), pinned
# at implementation time. The public ingest hostname is Cloudflare-proxied, so
# the origin firewall accepts 443 only from these ranges -- nothing can reach
# caddy around the proxy. The list changes rarely; refresh it here (and
# re-provision) when Cloudflare announces a change.
CLOUDFLARE_IPV4_RANGES: Final[tuple[str, ...]] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)
CLOUDFLARE_IPV6_RANGES: Final[tuple[str, ...]] = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

# Where each rendered artifact lands on the instance host (the deploy step
# stages via /tmp and sudo-installs; see remote_install.py).
OPENOBSERVE_ENV_FILE_PATH: Final[str] = "/etc/openobserve/openobserve.env"
CADDYFILE_PATH: Final[str] = "/etc/caddy/Caddyfile"
ORIGIN_CERTIFICATE_PATH: Final[str] = "/etc/caddy/origin.pem"
ORIGIN_PRIVATE_KEY_PATH: Final[str] = "/etc/caddy/origin.key"
NFTABLES_CONF_PATH: Final[str] = "/etc/nftables.conf"

# OpenObserve's on-host data directory: only the write path lives here (WAL +
# query cache); parquet data is in R2 and metadata in Neon, so the host is
# disposable.
OPENOBSERVE_DATA_DIR: Final[str] = "/var/lib/openobserve"

# WAL-to-object-store rotation, lowered from OpenObserve's 600s default so at
# most ~1 minute of acked data ever lives only on the instance disk (the
# durability envelope in the spec's "Storage and durability" section).
_MAX_FILE_RETENTION_TIME_SECONDS: Final[int] = 60


@pure
def env_file_quoted(value: str) -> str:
    """Double-quote one value for a systemd EnvironmentFile.

    systemd treats ``"`` as a quote terminator and ``\\`` as an escape inside
    double-quoted values, so both are escaped -- a Vault secret containing
    either must round-trip instead of silently corrupting the environment.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@pure
def render_openobserve_env(config: ObservabilityInstanceConfig) -> str:
    """Render the systemd EnvironmentFile configuring the OpenObserve process.

    Single-node local mode with remote storage: stream data as parquet in the
    tier's R2 bucket, metadata in Neon Postgres, and only the short WAL window
    on local disk. The HTTP server binds loopback only -- caddy fronts the
    ingest routes and operators tunnel to the rest.
    """
    return f"""\
# Rendered by imbue.observability -- do not edit on the host; re-render and redeploy.
ZO_ROOT_USER_EMAIL={env_file_quoted(config.root_user_email)}
ZO_ROOT_USER_PASSWORD={env_file_quoted(config.root_user_password.get_secret_value())}
ZO_DATA_DIR="{OPENOBSERVE_DATA_DIR}"
ZO_LOCAL_MODE="true"
ZO_LOCAL_MODE_STORAGE="s3"
ZO_META_STORE="postgres"
ZO_META_POSTGRES_DSN={env_file_quoted(config.meta_postgres_dsn.get_secret_value())}
ZO_S3_SERVER_URL={env_file_quoted(str(config.r2_endpoint_url).rstrip("/"))}
ZO_S3_REGION_NAME="auto"
ZO_S3_PROVIDER="s3"
ZO_S3_ACCESS_KEY={env_file_quoted(config.r2_access_key_id)}
ZO_S3_SECRET_KEY={env_file_quoted(config.r2_secret_access_key.get_secret_value())}
ZO_S3_BUCKET_NAME={env_file_quoted(config.r2_bucket_name)}
ZO_HTTP_ADDR="127.0.0.1"
ZO_HTTP_PORT="{OPENOBSERVE_HTTP_PORT}"
ZO_TELEMETRY="false"
ZO_MAX_FILE_RETENTION_TIME="{_MAX_FILE_RETENTION_TIME_SECONDS}"
ZO_COMPACT_DATA_RETENTION_DAYS="{config.metrics_retention_days}"
"""


@pure
def render_caddyfile(config: ObservabilityInstanceConfig) -> str:
    """Render the caddy ingest gate: OTLP routes + /healthz only, everything else 404.

    TLS terminates here with the tier's Cloudflare origin certificate
    (the hostname is Cloudflare-proxied, Full-strict). The bare /v1/* matchers
    cover an exporter that appends the standard OTLP suffixes to a base URL
    with no path (Modal's integration takes a base endpoint URL); they rewrite
    onto the default organization's routes and stamp the Modal log stream
    header -- Modal is the only bare-path sender (the fleet collectors target
    /api/<org> and set their own stream-name), and Modal Secret keys must be
    valid environment variable names, so the hyphenated ``stream-name``
    header cannot ride in the workspace's OTEL_HEADER_* secret.
    """
    upstream = f"127.0.0.1:{OPENOBSERVE_HTTP_PORT}"
    org = OPENOBSERVE_ORGANIZATION
    return f"""\
# Rendered by imbue.observability -- do not edit on the host; re-render and redeploy.
# Machine-ingest gate for {config.telemetry_hostname} (tier {config.tier}): only
# the OTLP ingest routes and /healthz exist publicly. The UI and query API are
# reachable ONLY via an SSH tunnel to {upstream}.
{{
    admin off
    auto_https off
}}

https://{config.telemetry_hostname}:443 {{
    tls {ORIGIN_CERTIFICATE_PATH} {ORIGIN_PRIVATE_KEY_PATH}

    @healthz path /healthz
    handle @healthz {{
        reverse_proxy {upstream}
    }}

    @otlp path /api/{org}/v1/logs /api/{org}/v1/metrics /api/{org}/v1/traces
    handle @otlp {{
        reverse_proxy {upstream}
    }}

    @otlp_bare path /v1/logs /v1/metrics /v1/traces
    handle @otlp_bare {{
        rewrite * /api/{org}{{uri}}
        request_header stream-name "{MODAL_LOG_STREAM_NAME}"
        reverse_proxy {upstream}
    }}

    handle {{
        respond 404
    }}
}}
"""


@pure
def render_origin_firewall_conf(hostname: str, tier: str) -> str:
    """Render the origin firewall: SSH from anywhere, 443 from Cloudflare only, drop the rest.

    Shared by the OpenObserve and Bugsink instance hosts -- both are
    Cloudflare-proxied ingest gates, so no legitimate client ever dials the
    origin's 443 directly; restricting it to Cloudflare's published ranges
    means the origin cannot be reached around the proxy (and the UI surface
    behind caddy's 404 is doubly unreachable).
    """
    ipv4_set = ", ".join(CLOUDFLARE_IPV4_RANGES)
    ipv6_set = ", ".join(CLOUDFLARE_IPV6_RANGES)
    return f"""\
#!/usr/sbin/nft -f
# Rendered by imbue.observability -- do not edit on the host; re-render and redeploy.
# Origin firewall for {hostname} (tier {tier}): default-deny
# inbound; SSH for operators, 443 from Cloudflare's edge ranges only.

flush ruleset

table inet observability {{
    set cloudflare_v4 {{
        type ipv4_addr
        flags interval
        elements = {{ {ipv4_set} }}
    }}

    set cloudflare_v6 {{
        type ipv6_addr
        flags interval
        elements = {{ {ipv6_set} }}
    }}

    chain input {{
        type filter hook input priority filter; policy drop;

        iif "lo" accept
        ct state established,related accept
        ct state invalid drop

        ip protocol icmp icmp type {{ echo-request, destination-unreachable, time-exceeded }} accept
        ip6 nexthdr ipv6-icmp accept

        tcp dport 22 accept

        ip saddr @cloudflare_v4 tcp dport 443 accept
        ip6 saddr @cloudflare_v6 tcp dport 443 accept
    }}
}}
"""


@pure
def render_nftables_conf(config: ObservabilityInstanceConfig) -> str:
    return render_origin_firewall_conf(str(config.telemetry_hostname), str(config.tier))


@pure
def render_all_instance_artifacts(config: ObservabilityInstanceConfig) -> dict[str, str]:
    """All rendered instance config artifacts, keyed by their on-disk basename.

    The single source of truth for which config files the instance host
    carries; the SSH deploy stages exactly these (the basenames must appear in
    ``remote_install.REMOTE_ARTIFACT_PATHS``). The self-monitoring collector's
    config is rendered separately (see ``collector_install``).
    """
    return {
        "openobserve.env": render_openobserve_env(config),
        "Caddyfile": render_caddyfile(config),
        "nftables.conf": render_nftables_conf(config),
        "origin.pem": config.origin_tls_certificate_pem,
        "origin.key": config.origin_tls_private_key_pem.get_secret_value(),
    }
