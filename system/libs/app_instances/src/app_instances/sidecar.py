import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Final

from app_manifest.errors import ManifestLoadError
from app_manifest.manifest import AppManifest, load_manifest
from app_manifest.primitives import AppUrl, InstancesUrl
from flask import Flask
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from loguru import logger
from werkzeug.serving import BaseWSGIServer, make_server

from app_instances.blueprint import build_instances_blueprint
from app_instances.errors import SidecarError
from app_instances.interfaces import InstanceNudgerInterface, InstanceSourceInterface
from app_instances.nudge import ShellNudger, shell_base_url

# The registration script, relative to the repo root every supervised program runs from.
FORWARD_PORT_SCRIPT: Final[Path] = Path("system/scripts/forward_port.py")

# Registration is one local file write; past the first threshold it is suspicious, past the
# second it is broken.
REGISTRATION_SLOW_SECONDS: Final[float] = 2.0
REGISTRATION_TIMEOUT_SECONDS: Final[float] = 15.0

SERVER_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0

# The shell convention for a process killed by a signal: 128 plus the signal number.
SIGNAL_EXIT_CODE_BASE: Final[int] = 128

_FORWARDED_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)


@pure
def child_exit_code(returncode: int) -> int:
    """A ``Popen`` return code as a process exit status: a signal death becomes 128 plus the signal number."""
    if returncode < 0:
        return SIGNAL_EXIT_CODE_BASE - returncode
    return returncode


@pure
def split_instances_url(instances_url: InstancesUrl) -> tuple[str, int]:
    """The host and port an instances URL names."""
    parts = urllib.parse.urlsplit(instances_url)
    if parts.hostname is None or parts.port is None:
        raise SidecarError(f"instances URL {instances_url!r} names no host and port")
    return parts.hostname, parts.port


def build_sidecar_app(
    source: InstanceSourceInterface, nudger: InstanceNudgerInterface
) -> Flask:
    """A Flask app that serves nothing but the instances blueprint."""
    app = Flask(__name__)
    app.register_blueprint(build_instances_blueprint(source, nudger))
    return app


def register_app(manifest_path: Path, app_url: AppUrl) -> None:
    """Upsert the app's registry row through ``forward_port.py --manifest``; raises SidecarError when that fails."""
    command = [
        sys.executable,
        str(FORWARD_PORT_SCRIPT),
        "--manifest",
        str(manifest_path),
        "--url",
        app_url,
    ]
    if not FORWARD_PORT_SCRIPT.is_file():
        raise SidecarError(
            f"registration script {FORWARD_PORT_SCRIPT} not found; the sidecar must run from the repo root"
        )
    started_at = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=REGISTRATION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise SidecarError(
            f"registration of {manifest_path} did not finish within {REGISTRATION_TIMEOUT_SECONDS}s"
        ) from e
    elapsed = time.monotonic() - started_at
    if completed.returncode != 0:
        raise SidecarError(
            f"registration of {manifest_path} failed with exit code {completed.returncode}: {completed.stderr.strip()}"
        )
    if elapsed > REGISTRATION_SLOW_SECONDS:
        logger.warning("Registered {} slowly, in {:.1f}s", manifest_path, elapsed)


def _load_sidecar_manifest(
    manifest_path: Path, instances_url: InstancesUrl
) -> AppManifest:
    """The manifest, checked to declare the instances API at the port this sidecar will serve."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestLoadError as e:
        raise SidecarError(str(e)) from e
    if not manifest.instances:
        raise SidecarError(
            f"manifest {manifest_path} does not declare instances = true"
        )
    if manifest.instances_url != instances_url:
        raise SidecarError(
            f"manifest {manifest_path} declares instances_url {manifest.instances_url!r}, "
            f"but the sidecar was asked to serve {instances_url!r}"
        )
    return manifest


def _start_server(
    host: str, port: int, app: Flask
) -> tuple[BaseWSGIServer, threading.Thread]:
    """Bind the instances API and serve it on a daemon thread; the socket accepts connections once this returns."""
    try:
        server = make_server(host, port, app, threaded=True)
    except OSError as e:
        raise SidecarError(
            f"cannot bind the instances API to {host}:{port}: {e}"
        ) from e
    server_thread = threading.Thread(
        target=server.serve_forever, name="instances-api", daemon=True
    )
    server_thread.start()
    return server, server_thread


def _stop_server(server: BaseWSGIServer, server_thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=SERVER_SHUTDOWN_TIMEOUT_SECONDS)


def _forward_signals_to(child: subprocess.Popen[bytes]) -> None:
    def forward(signum: int, _frame: FrameType | None) -> None:
        logger.debug("Forwarded signal {} to the child process {}", signum, child.pid)
        child.send_signal(signum)

    for forwarded_signal in _FORWARDED_SIGNALS:
        signal.signal(forwarded_signal, forward)


def run_sidecar(
    manifest_path: Path,
    app_url: AppUrl,
    instances_url: InstancesUrl,
    child_argv: Sequence[str],
    source: InstanceSourceInterface,
) -> int:
    """Serve the instances API beside a wrapped server, and return the exit status to end the program with.

    In order: the blueprint starts listening at ``instances_url`` (so the shell's first fetch after
    registration succeeds), the app is registered through ``forward_port.py --manifest`` with
    ``app_url``, the child is spawned, SIGTERM and SIGINT are forwarded to it, and its exit code
    (128 plus the signal number for a signal death) is returned once it ends. Must run on the main
    thread, which is where signal handlers can be installed.
    """
    if threading.current_thread() is not threading.main_thread():
        raise SidecarError(
            "run_sidecar must run on the main thread so it can forward signals"
        )
    manifest = _load_sidecar_manifest(manifest_path, instances_url)
    nudger = ShellNudger(app_name=manifest.name, shell_url=shell_base_url())
    host, port = split_instances_url(instances_url)
    with log_span(
        "Starting the instances API of {} at {}", manifest.name, instances_url
    ):
        server, server_thread = _start_server(
            host, port, build_sidecar_app(source, nudger)
        )
    try:
        with log_span("Registering {} at {}", manifest.name, app_url):
            register_app(manifest_path, app_url)
        with log_span(
            "Starting the wrapped server of {}: {}", manifest.name, list(child_argv)
        ):
            child = _spawn_child(child_argv)
        _forward_signals_to(child)
        returncode = child.wait()
    finally:
        _stop_server(server, server_thread)
    logger.debug(
        "Wrapped server of {} exited with return code {}", manifest.name, returncode
    )
    return child_exit_code(returncode)


def _spawn_child(child_argv: Sequence[str]) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(list(child_argv))
    except OSError as e:
        raise SidecarError(
            f"cannot start the wrapped server {list(child_argv)}: {e}"
        ) from e
