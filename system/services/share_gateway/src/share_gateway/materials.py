"""Share materials: the per-share files the minds app injects and the gateway's own state.

``data/.secrets/share.env`` is the gating material -- the gateway (and its
caddy + frpc children) run only while it is present and parseable. It is
written by the minds desktop app at share-enable and removed at unshare.
``data/.secrets/share_grants.toml`` (who may visit) lives next to it, and the
TLS key/cert plus the session-cookie signing secret persist under
``data/.secrets/`` so a re-share skips reprovisioning.
"""

import re
import secrets
from pathlib import Path

SECRETS_DIR = Path("data/.secrets")
MATERIALS_FILE = SECRETS_DIR / "share.env"
GRANTS_FILE = SECRETS_DIR / "share_grants.toml"
SIGNING_SECRET_FILE = SECRETS_DIR / "share_gateway_signing_key"
TLS_DIR = SECRETS_DIR / "share_tls"
TLS_KEY_FILE = TLS_DIR / "key.pem"
TLS_CERT_FILE = TLS_DIR / "cert.pem"

STATE_DIR = Path("data/.state/share_gateway")
CADDYFILE_PATH = STATE_DIR / "Caddyfile"
FRPC_CONFIG_PATH = STATE_DIR / "frpc.toml"

# Local port layout: caddy terminates the share's TLS on HTTPS_PORT (frpc
# splices relay bytes into it); the gateway's Flask app (forward_auth backend
# + /_auth/* endpoints) listens on GATEWAY_PORT.
GATEWAY_PORT = 8791
CADDY_HTTPS_PORT = 8443

_EXPORT_LINE_PATTERN = re.compile(r"""^export\s+([A-Z0-9_]+)=["']?([^"'\n]*)["']?\s*$""", re.MULTILINE)

_REQUIRED_KEYS = (
    "SHARE_WORKSPACE_DOMAIN",
    "SHARE_RELAY_ENDPOINT",
    "SHARE_RELAY_TOKEN",
    "SHARE_CONNECTOR_URL",
    "SHARE_BROKER_URL",
)


class ShareMaterials:
    """The parsed contents of ``share.env``."""

    def __init__(
        self,
        workspace_domain: str,
        relay_host: str,
        relay_port: int,
        relay_token: str,
        connector_url: str,
        broker_url: str,
    ) -> None:
        self.workspace_domain = workspace_domain
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.relay_token = relay_token
        self.connector_url = connector_url
        self.broker_url = broker_url

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ShareMaterials):
            return NotImplemented
        return vars(self) == vars(other)


def parse_share_materials(text: str) -> ShareMaterials | None:
    """Parse share.env text into materials; None when any required key is missing or malformed."""
    values = {match.group(1): match.group(2) for match in _EXPORT_LINE_PATTERN.finditer(text)}
    if any(not values.get(key) for key in _REQUIRED_KEYS):
        return None
    relay_endpoint = values["SHARE_RELAY_ENDPOINT"]
    relay_host, separator, relay_port_text = relay_endpoint.rpartition(":")
    if not separator or not relay_host or not relay_port_text.isdigit():
        return None
    return ShareMaterials(
        workspace_domain=values["SHARE_WORKSPACE_DOMAIN"].lower(),
        relay_host=relay_host,
        relay_port=int(relay_port_text),
        relay_token=values["SHARE_RELAY_TOKEN"],
        connector_url=values["SHARE_CONNECTOR_URL"].rstrip("/"),
        broker_url=values["SHARE_BROKER_URL"].rstrip("/"),
    )


def read_share_materials(path: Path) -> ShareMaterials | None:
    """Read + parse the materials file; None when absent or unusable."""
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    return parse_share_materials(text)


def load_or_create_signing_secret(path: Path) -> str:
    """The session-cookie signing secret, generated once and persisted with 0600."""
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret)
    path.chmod(0o600)
    return secret
