"""Share materials: the per-share files the minds app injects and the gateway's own state.

``data/.secrets/share.env`` is the gating material -- the gateway (and its
caddy + frpc children) run only while it is present and parseable. It is
written by the minds desktop app at share-enable and removed at unshare.
``data/.secrets/share_grants.toml`` (who may visit) lives next to it, and the
TLS key/cert persist under ``data/.secrets/`` so a re-share skips
reprovisioning. The session-cookie signing secret does not: unsharing deletes
it, so every session dies with the share, and the secret file also records the
SHA-256 of the relay token it was minted under, so a secret that outlived an
unshare the runner never saw is still replaced when the next share (which
always carries a new relay token) starts.
"""

import hashlib
import json
import re
import secrets
from pathlib import Path

SECRETS_DIR = Path("data/.secrets")
MATERIALS_FILE = SECRETS_DIR / "share.env"
GRANTS_FILE = SECRETS_DIR / "share_grants.toml"
SIGNING_SECRET_FILE = SECRETS_DIR / "share_gateway_signing_key"
AUTH_LABEL_FILE = SECRETS_DIR / "share_auth_label"
TLS_DIR = SECRETS_DIR / "share_tls"
TLS_KEY_FILE = TLS_DIR / "key.pem"
TLS_CERT_FILE = TLS_DIR / "cert.pem"

# The dedicated auth-origin label is ``auth-<rand>``; the same 8-char
# lowercase base36 suffix scheme service labels use (see forward_port.py).
_AUTH_LABEL_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_AUTH_LABEL_RANDOM_LENGTH = 8

STATE_DIR = Path("data/.state/share_gateway")
CADDYFILE_PATH = STATE_DIR / "Caddyfile"
# One frpc config per assigned relay lives at frpc-<relay_id>.toml under
# STATE_DIR (see frpc_config_path); the workspace tunnels to EVERY relay of
# its region. The last-fetched relay assignment is cached so a container
# restart brings the tunnels up without the connector.
ASSIGNMENT_CACHE_PATH = STATE_DIR / "assignment.json"

# Local port layout: caddy terminates the share's TLS on HTTPS_PORT (each
# relay's frpc splices its relay bytes into it); the gateway's Flask app
# (forward_auth backend + /_auth/* endpoints) listens on GATEWAY_PORT; each
# frpc's loopback admin server (for `frpc reload`) listens on
# FRPC_ADMIN_PORT_BASE + its slot index.
GATEWAY_PORT = 8791
CADDY_HTTPS_PORT = 8443
FRPC_ADMIN_PORT_BASE = 7401


def frpc_config_path(relay_id: str) -> Path:
    return STATE_DIR / f"frpc-{relay_id}.toml"


_EXPORT_LINE_PATTERN = re.compile(r"""^export\s+([A-Z0-9_]+)=["']?([^"'\n]*)["']?\s*$""", re.MULTILINE)

# No relay endpoint here: the gateway fetches its relay assignment from the
# connector (relay-token auth) and re-polls, so fleet changes never require
# re-injecting materials.
_REQUIRED_KEYS = (
    "SHARE_WORKSPACE_DOMAIN",
    "SHARE_RELAY_TOKEN",
    "SHARE_CONNECTOR_URL",
    "SHARE_BROKER_URL",
)


class ShareMaterials:
    """The parsed contents of ``share.env``."""

    def __init__(
        self,
        workspace_domain: str,
        relay_token: str,
        connector_url: str,
        broker_url: str,
        chrome_origin: str,
    ) -> None:
        self.workspace_domain = workspace_domain
        self.relay_token = relay_token
        self.connector_url = connector_url
        self.broker_url = broker_url
        # The hosted minds chrome origin (e.g. https://minds.imbue.com) allowed
        # to embed this workspace in an iframe and probe /_health cross-origin.
        # Empty when the share was created by an older client that did not set
        # SHARE_CHROME_ORIGIN -- embedding + CORS then stay disabled.
        self.chrome_origin = chrome_origin

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ShareMaterials):
            return NotImplemented
        return vars(self) == vars(other)


def parse_share_materials(text: str) -> ShareMaterials | None:
    """Parse share.env text into materials; None when any required key is missing or malformed."""
    values = {match.group(1): match.group(2) for match in _EXPORT_LINE_PATTERN.finditer(text)}
    if any(not values.get(key) for key in _REQUIRED_KEYS):
        return None
    return ShareMaterials(
        workspace_domain=values["SHARE_WORKSPACE_DOMAIN"].lower(),
        relay_token=values["SHARE_RELAY_TOKEN"],
        connector_url=values["SHARE_CONNECTOR_URL"].rstrip("/"),
        broker_url=values["SHARE_BROKER_URL"].rstrip("/"),
        # Optional: absent for shares created before the hosted-chrome rollout.
        chrome_origin=values.get("SHARE_CHROME_ORIGIN", "").rstrip("/"),
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


def _relay_token_digest(relay_token: str) -> str:
    return hashlib.sha256(relay_token.encode()).hexdigest()


def _read_signing_secret_bound_to(path: Path, relay_token_digest: str) -> str | None:
    """The stored secret when the file is readable and was minted under this digest; None otherwise."""
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    secret = stored.get("secret")
    if not isinstance(secret, str) or not secret:
        return None
    if stored.get("relay_token_sha256") != relay_token_digest:
        return None
    return secret


def load_or_create_signing_secret(path: Path, relay_token: str) -> str:
    """The session-cookie signing secret for the share ``relay_token`` belongs to, persisted with 0600.

    The file records the SHA-256 of the relay token the secret was minted under. A stored secret is
    reused only under that same token; every share mints a new relay token, so a secret left behind by
    an earlier share (an unshare and re-share the runner was down for) is replaced rather than reused,
    and the earlier share's cookies stop verifying. An unreadable or unbound file is replaced the same way.
    """
    relay_token_digest = _relay_token_digest(relay_token)
    existing = _read_signing_secret_bound_to(path, relay_token_digest)
    if existing is not None:
        return existing
    secret = secrets.token_urlsafe(48)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"secret": secret, "relay_token_sha256": relay_token_digest}))
    path.chmod(0o600)
    return secret


def discard_signing_secret(path: Path) -> bool:
    """Delete the signing secret so every session it signed (the owner's included) stops verifying; False when there
    was none to delete."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


_VALID_AUTH_LABEL = re.compile(r"^auth-[a-z0-9]{" + str(_AUTH_LABEL_RANDOM_LENGTH) + r"}$")


def load_or_create_auth_label(path: Path) -> str:
    """The workspace's dedicated ``auth-<rand>`` origin label, generated once and persisted.

    Stable across unshare/re-share (like the cert) so the auth origin does not
    move. A stored value that does not match the expected shape is replaced.
    """
    if path.exists():
        existing = path.read_text().strip()
        if _VALID_AUTH_LABEL.match(existing):
            return existing
    suffix = "".join(secrets.choice(_AUTH_LABEL_ALPHABET) for _ in range(_AUTH_LABEL_RANDOM_LENGTH))
    label = f"auth-{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(label)
    path.chmod(0o600)
    return label
