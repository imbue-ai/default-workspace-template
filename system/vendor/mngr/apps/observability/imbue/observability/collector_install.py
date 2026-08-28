"""Rendering one machine's OpenTelemetry Collector config + self-contained install script.

One pinned ``otelcol-contrib`` runs per bare-metal slice box, share-relay VPS,
and observability instance host: hostmetrics + the systemd journal, pushed
over OTLP HTTP to the tier's ingest hostname with a per-sender-class
credential and a file-backed sending queue (instance downtime buffers on the
sender instead of dropping).

The install script is deliberately self-contained (the rendered config rides
inside it as a heredoc) so every install path is the same single artifact: the
box prep flow (``minds-admin server prep`` / ``setup``) renders and appends it
in-process, the relay flow and the instance deploy run it over SSH.

The collector config is YAML because the OpenTelemetry Collector requires
YAML -- this is a third-party tool's mandated format (like cloud-init's), not
one of our config files.
"""

from typing import Final
from typing import assert_never

from imbue.imbue_common.pure import pure
from imbue.observability.data_types import CollectorInstallConfig
from imbue.observability.errors import ObservabilityError
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import LOG_STREAM_NAME_BY_COLLECTOR_ROLE
from imbue.observability.primitives import OPENOBSERVE_ORGANIZATION

# Pinned OpenTelemetry Collector (contrib) release installed on every machine
# (bump deliberately; the sha256s are the published checksums of this exact
# version's linux .deb packages).
OTELCOL_CONTRIB_VERSION: Final[str] = "0.159.0"
_OTELCOL_DEB_SHA256_BY_GOARCH: Final[dict[str, str]] = {
    "amd64": "4ede8d750d6bf845e353be46cc550f590e6ccdaeeb60aae941cde6ad561877db",
    "arm64": "430469fbfb48f123d08dfc896973bdc205ba393901cc506e92c9c928698a6d5e",
}

# Where the .deb's own systemd service reads its config, and where the
# file-backed sending queue lives.
OTELCOL_CONFIG_PATH: Final[str] = "/etc/otelcol-contrib/config.yaml"
_OTELCOL_QUEUE_DIR: Final[str] = "/var/lib/otelcol/queue"

# The hard memory cap: the collector must never compete with customer slices
# (boxes) or the store itself (the instance host) for memory.
_MEMORY_LIMIT_MIB: Final[int] = 256
_MEMORY_SPIKE_LIMIT_MIB: Final[int] = 64

_HOSTMETRICS_COLLECTION_INTERVAL_SECONDS: Final[int] = 60

# How long a failed export batch keeps retrying before the queue drops it.
# Long enough to ride out an instance replacement (a few minutes of planned
# downtime), bounded so a poison batch cannot wedge the queue forever.
_EXPORT_RETRY_MAX_ELAPSED_SECONDS: Final[int] = 1800


class CollectorConfigRenderError(ObservabilityError):
    """Raised when a value cannot be embedded safely in the rendered collector config."""


@pure
def _yaml_double_quoted(value: str) -> str:
    """Double-quote one value for embedding in the rendered YAML config.

    ``\\`` and ``"`` are YAML escape/terminator characters inside a
    double-quoted scalar, so both are escaped -- a Vault credential containing
    either must round-trip instead of breaking (or silently corrupting) the
    config the collector parses on the remote host. Control characters are
    rejected loudly: an Authorization header value can never legitimately
    contain them, and YAML line folding would corrupt them silently.
    """
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise CollectorConfigRenderError(
            "Ingest credential contains a control character (e.g. a newline); it must be a "
            "single-line Authorization header value like 'Basic <base64(email:password)>'."
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@pure
def _process_name_patterns_for_role(role: CollectorRole) -> tuple[str, ...]:
    """The per-process scraper's include patterns: the processes worth watching on each machine class.

    Box qemu processes double as the per-slice CPU/memory signal -- the agreed
    substitute for any in-guest collector (which is explicitly forbidden).
    """
    match role:
        case CollectorRole.BOX:
            return ("^qemu.*", "^lima.*", "^sshd$")
        case CollectorRole.RELAY:
            return ("^frps$", "^caddy$", "^sshd$")
        case CollectorRole.INSTANCE:
            return ("^openobserve$", "^caddy$", "^otelcol.*", "^sshd$")
        case _ as unreachable:
            assert_never(unreachable)


@pure
def render_collector_config(config: CollectorInstallConfig) -> str:
    """Render the otelcol-contrib config for one machine.

    Two pipelines: hostmetrics (plus per-process metrics for the role's
    interesting processes) and the systemd journal, both stamped with the tier
    and role, exported over OTLP HTTP to the default organization's routes
    with the sender-class credential. The ``stream-name`` header names the log
    stream OpenObserve files journal lines into (per-stream retention hangs
    off it); OpenObserve ignores it for metrics.
    """
    process_patterns = ", ".join(f'"{pattern}"' for pattern in _process_name_patterns_for_role(config.role))
    endpoint = f"{str(config.ingest_url).rstrip('/')}/api/{OPENOBSERVE_ORGANIZATION}"
    log_stream_name = LOG_STREAM_NAME_BY_COLLECTOR_ROLE[config.role]
    role_label = str(config.role).lower()
    return f"""\
# Rendered by imbue.observability -- do not edit on the host; re-render and reinstall.
extensions:
  file_storage:
    directory: {_OTELCOL_QUEUE_DIR}
    create_directory: true

receivers:
  hostmetrics:
    collection_interval: {_HOSTMETRICS_COLLECTION_INTERVAL_SECONDS}s
    scrapers:
      cpu:
      memory:
      load:
      disk:
      filesystem:
      network:
      processes:
      process:
        include:
          match_type: regexp
          names: [{process_patterns}]
        mute_process_exe_error: true
        mute_process_io_error: true
        mute_process_user_error: true
  journald:
    priority: info

processors:
  memory_limiter:
    check_interval: 5s
    limit_mib: {_MEMORY_LIMIT_MIB}
    spike_limit_mib: {_MEMORY_SPIKE_LIMIT_MIB}
  resourcedetection:
    detectors: [system]
    system:
      hostname_sources: [os]
  resource:
    attributes:
      - key: deployment.environment
        value: "{config.tier}"
        action: upsert
      - key: imbue.role
        value: "{role_label}"
        action: upsert
  batch:
    timeout: 10s

exporters:
  otlphttp:
    endpoint: "{endpoint}"
    headers:
      Authorization: {_yaml_double_quoted(config.ingest_authorization_header_value.get_secret_value())}
      stream-name: "{log_stream_name}"
    retry_on_failure:
      enabled: true
      max_elapsed_time: {_EXPORT_RETRY_MAX_ELAPSED_SECONDS}s
    sending_queue:
      enabled: true
      storage: file_storage

service:
  extensions: [file_storage]
  pipelines:
    metrics:
      receivers: [hostmetrics]
      processors: [memory_limiter, resourcedetection, resource, batch]
      exporters: [otlphttp]
    logs:
      receivers: [journald]
      processors: [memory_limiter, resourcedetection, resource, batch]
      exporters: [otlphttp]
"""


@pure
def render_collector_install_script(config: CollectorInstallConfig) -> str:
    """The idempotent root install script: pinned otelcol-contrib .deb, embedded config, service restart.

    Self-contained on purpose: the rendered config rides inside as a quoted
    heredoc so box prep (rendered in-process by ``minds-admin server prep`` /
    ``setup``), relay installs, and the instance deploy all run the exact same
    artifact. The config embeds the ingest credential, so it is installed
    owner-only for the service user.
    """
    amd64_sha256 = _OTELCOL_DEB_SHA256_BY_GOARCH["amd64"]
    arm64_sha256 = _OTELCOL_DEB_SHA256_BY_GOARCH["arm64"]
    version = OTELCOL_CONTRIB_VERSION
    config_text = render_collector_config(config)
    return f"""\
set -euo pipefail

# --- observability collector install (rendered by imbue.observability) ---

# Fresh VPSes (relays, the instance host) get this script while first boot may
# still be installing packages; wait it out so dpkg/curl below never race
# cloud-init. Tolerate a degraded/absent cloud-init (`|| true`): long-prepped
# bare-metal boxes may not have it at all, and any real missing dependency
# still fails loudly right after.
cloud-init status --wait 2>/dev/null || true

otelcol_arch="$(uname -m)"
case "${{otelcol_arch}}" in
    x86_64) otelcol_goarch="amd64"; otelcol_sha256="{amd64_sha256}" ;;
    aarch64) otelcol_goarch="arm64"; otelcol_sha256="{arm64_sha256}" ;;
    *) echo "Unsupported architecture for otelcol-contrib: ${{otelcol_arch}}" >&2; exit 1 ;;
esac

if ! dpkg-query -W -f='${{Version}}' otelcol-contrib 2>/dev/null | grep -q "^{version}"; then
    # Download and verify inside a private root-owned dir: fixed /tmp paths
    # would let a local user pre-plant a symlink or swap the package between
    # the sha256 check and the install.
    otelcol_tmp="$(mktemp -d)"
    curl -fsSL "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v{version}/otelcol-contrib_{version}_linux_${{otelcol_goarch}}.deb" -o "${{otelcol_tmp}}/otelcol-contrib.deb"
    echo "${{otelcol_sha256}}  ${{otelcol_tmp}}/otelcol-contrib.deb" | sha256sum -c -
    # apt-get (not dpkg -i): its lock timeout rides out a concurrent
    # unattended-upgrades run, which holds the dpkg lock for minutes on a
    # freshly booted VPS (observed live on the production bring-up) and is
    # not covered by the cloud-init wait above.
    apt-get install -y -o DPkg::Lock::Timeout=600 "${{otelcol_tmp}}/otelcol-contrib.deb"
    rm -rf "${{otelcol_tmp}}"
fi

# The journald receiver shells out to journalctl, which needs journal read
# access; the queue dir backs the file_storage sending queue across restarts.
usermod -aG systemd-journal otelcol-contrib
install -d -m 0750 -o otelcol-contrib -g otelcol-contrib /var/lib/otelcol "{_OTELCOL_QUEUE_DIR}"

otelcol_config_tmp="$(mktemp)"
cat > "${{otelcol_config_tmp}}" <<'OTELCOL_CONFIG_EOF'
{config_text}OTELCOL_CONFIG_EOF
install -m 0600 -o otelcol-contrib -g otelcol-contrib "${{otelcol_config_tmp}}" "{OTELCOL_CONFIG_PATH}"
rm -f "${{otelcol_config_tmp}}"

systemctl daemon-reload
systemctl enable otelcol-contrib
systemctl restart otelcol-contrib
"""
