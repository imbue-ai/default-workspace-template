"""Entry point for the owner-exec service.

Runs a loopback HTTP server and registers its port so the desktop forward and
the share gateway can route to it. The audience (this workspace's share
domain) is read from ``data/.secrets/share.env`` on each request via the app
config's resolver, so enabling/disabling sharing takes effect without a
restart.
"""

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger
from werkzeug.serving import make_server

from owner_exec.server import OwnerExecConfig
from owner_exec.server import build_owner_exec_app
from owner_exec.signing import NonceCache
from owner_exec.signing import current_unix_time

SERVICE_NAME = "owner-exec"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8793

_REPO_ROOT = Path(os.environ.get("MINDS_WORKSPACE_ROOT", "/home/user/workspace"))
_SHARE_ENV_PATH = _REPO_ROOT / "data" / ".secrets" / "share.env"
_AUTHORIZED_KEYS_PATH = Path.home() / ".ssh" / "authorized_keys"

_SHARE_WORKSPACE_DOMAIN_KEY = "SHARE_WORKSPACE_DOMAIN"
_SHARE_CHROME_ORIGIN_KEY = "SHARE_CHROME_ORIGIN"


def _read_share_env_value(share_env_path: Path, wanted_key: str) -> str:
    """Read one exported value from share.env, or '' when absent/unreadable."""
    if not share_env_path.exists():
        return ""
    try:
        text = share_env_path.read_text()
    except OSError:
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("export "):
            continue
        assignment = line[len("export ") :]
        key, separator, value = assignment.partition("=")
        if separator and key.strip() == wanted_key:
            return value.strip().strip("\"'")
    return ""


def read_share_audience(share_env_path: Path) -> str:
    """Read the workspace's share domain from share.env, or '' when not shared."""
    return _read_share_env_value(share_env_path, _SHARE_WORKSPACE_DOMAIN_KEY).lower()


def read_share_chrome_origin(share_env_path: Path) -> str:
    """Read the hosted chrome origin from share.env, or '' when none is configured."""
    return _read_share_env_value(share_env_path, _SHARE_CHROME_ORIGIN_KEY).rstrip("/")


def _register_port() -> None:
    """Register the owner-exec port in apps.toml so forward + share route to it.

    Best-effort: a registration hiccup (the helper script is missing, the
    interpreter can't be spawned, the repo root is momentarily unavailable
    during a restore's restart) must never take the exec service down --
    supervisord would report that as a spawn error and fail the whole
    ``restart all``. The service is still useful without the apps.toml row
    (the row only affects forward/share routing, not the loopback listener),
    and the next boot re-registers.
    """
    forward_port_script = _REPO_ROOT / "system" / "scripts" / "forward_port.py"
    cwd = str(_REPO_ROOT) if _REPO_ROOT.is_dir() else None
    try:
        subprocess.run(
            [
                sys.executable,
                str(forward_port_script),
                "--name",
                SERVICE_NAME,
                "--url",
                f"http://{LISTEN_HOST}:{LISTEN_PORT}",
                "--internal",
            ],
            cwd=cwd,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Port registration failed (continuing without it): {}", exc)


def main() -> None:
    _register_port()
    config = OwnerExecConfig(
        # Read the audience fresh from share.env per request so a re-share is
        # picked up without a restart.
        audience_resolver=lambda: read_share_audience(_SHARE_ENV_PATH),
        authorized_keys_path=_AUTHORIZED_KEYS_PATH,
        repo_root=_REPO_ROOT,
        nonce_cache=NonceCache(),
        now=current_unix_time,
        chrome_origin_resolver=lambda: read_share_chrome_origin(_SHARE_ENV_PATH),
    )
    app = build_owner_exec_app(config)
    server = make_server(LISTEN_HOST, LISTEN_PORT, app, threaded=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
