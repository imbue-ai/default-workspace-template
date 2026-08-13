"""Share gateway runner: the supervisord entrypoint that manages the whole share stack.

Watches ``data/.secrets/share.env`` (the share materials the minds app injects)
plus ``data/.state/apps.toml`` (the service registry). While materials are
present it keeps the stack up: TLS key/CSR/cert via the connector, the
rendered Caddyfile + frpc.toml, caddy + frpc child processes, and the gateway
Flask server caddy's forward_auth consults. When the materials disappear
(unshare) the children stop and the tunnel drops; key, cert, and the cookie
signing secret stay on disk for a fast re-share.

Same watch idiom as the other material-gated services: inotify when available,
10-second mtime polling as the fallback.
"""

import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import httpx
from werkzeug.serving import BaseWSGIServer
from werkzeug.serving import make_server

from share_gateway import materials as materials_module
from share_gateway.log import log as _log
from share_gateway.caddyfile import build_label_to_name
from share_gateway.caddyfile import read_registered_apps
from share_gateway.caddyfile import render_caddyfile
from share_gateway.certs import CertProvisioningError
from share_gateway.certs import ensure_share_certificate
from share_gateway.frpc_config import render_frpc_toml
from share_gateway.handoff import JwksCache
from share_gateway.handoff import SingleUseJtiRegistry
from share_gateway.materials import ShareMaterials
from share_gateway.materials import load_or_create_auth_label
from share_gateway.materials import load_or_create_signing_secret
from share_gateway.materials import read_share_materials
from share_gateway.server import PendingLoginRegistry
from share_gateway.server import build_gateway_app

POLL_INTERVAL_SECONDS = 10
APPS_TOML_PATH = Path("data/.state/apps.toml")
_CADDY_ADMIN_URL = "http://localhost:2019"
_RENEWAL_CHECK_INTERVAL = timedelta(hours=24)


def _try_setup_inotify(paths: list[Path]) -> object | None:
    """Watch the parent directories of every gating file; None when inotify is unavailable."""
    try:
        import inotifyx  # type: ignore[import-untyped]

        fd = inotifyx.init()
        for parent in {path.parent for path in paths}:
            parent.mkdir(parents=True, exist_ok=True)
            inotifyx.add_watch(
                fd,
                str(parent),
                inotifyx.IN_MODIFY
                | inotifyx.IN_CREATE
                | inotifyx.IN_MOVED_TO
                | inotifyx.IN_DELETE
                | inotifyx.IN_MOVED_FROM,
            )
        return fd
    except (ImportError, OSError):
        return None


def _wait_for_change_inotify(fd: object, timeout_seconds: float) -> bool:
    try:
        import inotifyx  # type: ignore[import-untyped]

        events = inotifyx.get_events(fd, timeout_seconds)
        return len(events) > 0
    except (ImportError, OSError):
        return False


def _stop_child(process: subprocess.Popen[bytes] | None, name: str) -> None:
    if process is None or process.poll() is not None:
        return
    _log(f"Stopping {name}...")
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _start_child(argv: list[str], name: str) -> subprocess.Popen[bytes]:
    _log(f"Starting {name}: {' '.join(argv[:3])}...")
    return subprocess.Popen(argv, stdout=sys.stderr.fileno(), stderr=sys.stderr.fileno())


def _reload_caddy(caddyfile_text: str) -> bool:
    """Push the rendered Caddyfile through caddy's admin API; False when caddy is unreachable."""
    try:
        response = httpx.post(
            f"{_CADDY_ADMIN_URL}/load",
            content=caddyfile_text,
            headers={"Content-Type": "text/caddyfile"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        _log(f"caddy admin reload failed: {exc}")
        return False
    if response.status_code >= 400:
        _log(f"caddy admin reload rejected ({response.status_code}): {response.text[:300]}")
        return False
    return True


def _reload_frpc() -> bool:
    """Hot-reload frpc's proxies from its on-disk config via its admin API; False on failure."""
    try:
        result = subprocess.run(
            ["frpc", "reload", "-c", str(materials_module.FRPC_CONFIG_PATH)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"frpc reload failed: {exc}")
        return False
    if result.returncode != 0:
        _log(f"frpc reload rejected ({result.returncode}): {result.stdout.decode(errors='replace')[:300]}")
        return False
    return True


class ShareStack:
    """The running pieces of one active share: gateway server, caddy, frpc."""

    def __init__(self, materials: ShareMaterials, auth_label: str) -> None:
        self.materials = materials
        self.auth_label = auth_label
        self.gateway_server: BaseWSGIServer | None = None
        self.caddy_process: subprocess.Popen[bytes] | None = None
        self.frpc_process: subprocess.Popen[bytes] | None = None
        self.last_caddyfile_text = ""
        self.last_frpc_config_text = ""
        self.last_renewal_check = datetime.now(timezone.utc)

    def render_current_caddyfile(self) -> str:
        return render_caddyfile(
            workspace_domain=self.materials.workspace_domain,
            apps=read_registered_apps(APPS_TOML_PATH),
            auth_label=self.auth_label,
            tls_cert_path=materials_module.TLS_CERT_FILE.resolve(),
            tls_key_path=materials_module.TLS_KEY_FILE.resolve(),
            https_port=materials_module.CADDY_HTTPS_PORT,
            gateway_port=materials_module.GATEWAY_PORT,
            chrome_origin=self.materials.chrome_origin,
        )

    def render_current_frpc_config(self) -> str:
        return render_frpc_toml(
            relay_host=self.materials.relay_host,
            relay_port=self.materials.relay_port,
            relay_token=self.materials.relay_token,
            workspace_domain=self.materials.workspace_domain,
            service_labels=[app.label for app in read_registered_apps(APPS_TOML_PATH)],
            auth_label=self.auth_label,
            local_https_port=materials_module.CADDY_HTTPS_PORT,
            admin_port=materials_module.FRPC_ADMIN_PORT,
        )


def _start_stack(materials: ShareMaterials) -> ShareStack | None:
    """Provision (cert) and start (gateway thread, caddy, frpc) one share's stack."""
    try:
        ensure_share_certificate(
            key_path=materials_module.TLS_KEY_FILE,
            cert_path=materials_module.TLS_CERT_FILE,
            workspace_domain=materials.workspace_domain,
            connector_url=materials.connector_url,
            relay_token=materials.relay_token,
        )
    except CertProvisioningError as exc:
        _log(f"certificate provisioning failed (will retry on next change/poll): {exc}")
        return None

    auth_label = load_or_create_auth_label(materials_module.AUTH_LABEL_FILE)
    stack = ShareStack(materials, auth_label)
    signing_secret = load_or_create_signing_secret(materials_module.SIGNING_SECRET_FILE)
    app = build_gateway_app(
        materials=materials,
        grants_path=materials_module.GRANTS_FILE,
        signing_secret=signing_secret,
        jwks_cache=JwksCache(f"{materials.broker_url}/share/jwks.json"),
        jti_registry=SingleUseJtiRegistry(),
        pending_logins=PendingLoginRegistry(),
        auth_label=auth_label,
        get_label_to_name=lambda: build_label_to_name(read_registered_apps(APPS_TOML_PATH)),
    )
    stack.gateway_server = make_server("127.0.0.1", materials_module.GATEWAY_PORT, app, threaded=True)
    threading.Thread(target=stack.gateway_server.serve_forever, name="share-gateway-http", daemon=True).start()

    materials_module.STATE_DIR.mkdir(parents=True, exist_ok=True)
    stack.last_caddyfile_text = stack.render_current_caddyfile()
    materials_module.CADDYFILE_PATH.write_text(stack.last_caddyfile_text)
    stack.caddy_process = _start_child(
        ["caddy", "run", "--config", str(materials_module.CADDYFILE_PATH), "--adapter", "caddyfile"], "caddy"
    )

    stack.last_frpc_config_text = stack.render_current_frpc_config()
    materials_module.FRPC_CONFIG_PATH.write_text(stack.last_frpc_config_text)
    stack.frpc_process = _start_child(["frpc", "-c", str(materials_module.FRPC_CONFIG_PATH)], "frpc")
    _log(f"Share stack up for {materials.workspace_domain}")
    return stack


def _stop_stack(stack: ShareStack | None) -> None:
    if stack is None:
        return
    _stop_child(stack.frpc_process, "frpc")
    _stop_child(stack.caddy_process, "caddy")
    if stack.gateway_server is not None:
        stack.gateway_server.shutdown()
    _log("Share stack stopped")


def _tick_running_stack(stack: ShareStack) -> ShareStack | None:
    """Periodic upkeep for a running stack: restart dead children, re-render caddy, renew the cert."""
    if stack.caddy_process is not None and stack.caddy_process.poll() is not None:
        _log(f"caddy exited with {stack.caddy_process.returncode}; restarting the stack")
        _stop_stack(stack)
        return None
    if stack.frpc_process is not None and stack.frpc_process.poll() is not None:
        _log(f"frpc exited with {stack.frpc_process.returncode}; restarting the stack")
        _stop_stack(stack)
        return None

    current_caddyfile = stack.render_current_caddyfile()
    if current_caddyfile != stack.last_caddyfile_text:
        materials_module.CADDYFILE_PATH.write_text(current_caddyfile)
        if _reload_caddy(current_caddyfile):
            stack.last_caddyfile_text = current_caddyfile
            _log("Reloaded caddy for a service-registry change")

    # A service registered while shared needs its label claimed on the relay
    # too; hot-reload frpc (admin API) so the new claim lands without dropping
    # existing viewers' tunnels.
    current_frpc_config = stack.render_current_frpc_config()
    if current_frpc_config != stack.last_frpc_config_text:
        materials_module.FRPC_CONFIG_PATH.write_text(current_frpc_config)
        if _reload_frpc():
            stack.last_frpc_config_text = current_frpc_config
            _log("Reloaded frpc for a service-registry change")

    now = datetime.now(timezone.utc)
    if now - stack.last_renewal_check >= _RENEWAL_CHECK_INTERVAL:
        stack.last_renewal_check = now
        cert_before = materials_module.TLS_CERT_FILE.read_text() if materials_module.TLS_CERT_FILE.exists() else ""
        try:
            ensure_share_certificate(
                key_path=materials_module.TLS_KEY_FILE,
                cert_path=materials_module.TLS_CERT_FILE,
                workspace_domain=stack.materials.workspace_domain,
                connector_url=stack.materials.connector_url,
                relay_token=stack.materials.relay_token,
            )
        except CertProvisioningError as exc:
            _log(f"certificate renewal check failed (will retry tomorrow): {exc}")
        else:
            cert_after = materials_module.TLS_CERT_FILE.read_text()
            if cert_after != cert_before:
                _reload_caddy(stack.last_caddyfile_text)
                _log("Renewed the share certificate and reloaded caddy")
    return stack


def main() -> None:
    _log(f"Watching {materials_module.MATERIALS_FILE} for share materials")
    materials_module.SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    inotify_fd = _try_setup_inotify([materials_module.MATERIALS_FILE, APPS_TOML_PATH])
    stack: ShareStack | None = None

    def _handle_signal(signum: int, frame: object) -> None:
        _stop_stack(stack)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while True:
        materials = read_share_materials(materials_module.MATERIALS_FILE)

        if materials is None and stack is not None:
            _log("Share materials removed; tearing the stack down")
            _stop_stack(stack)
            stack = None
        elif materials is not None and stack is None:
            stack = _start_stack(materials)
        elif materials is not None and stack is not None and materials != stack.materials:
            _log("Share materials changed; restarting the stack")
            _stop_stack(stack)
            stack = _start_stack(materials)
        elif stack is not None:
            stack = _tick_running_stack(stack)
        else:
            pass

        if inotify_fd is not None:
            _wait_for_change_inotify(inotify_fd, POLL_INTERVAL_SECONDS)
        else:
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
