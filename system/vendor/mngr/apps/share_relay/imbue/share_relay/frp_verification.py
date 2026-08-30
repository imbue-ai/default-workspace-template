"""Manual verification harness for the frp behaviors the multi-relay design rests on.

Run on every frp version bump (before changing FRP_VERSION in
remote_install.py) with::

    uv run python -m imbue.share_relay.frp_verification

It downloads the pinned frp release (sha256-verified), runs local frps/frpc
processes on loopback ports, and asserts the behaviors phase 1 of
blueprint/multi-relay depends on:

1. An unknown SNI gets an instant fatal ``unrecognized_name`` TLS alert plus a
   clean FIN (never a hang) -- the DNS-steering miss case is a fast error.
2. The frps vhost does NOT accept inbound PROXY protocol -- which is why no
   fronting router exists in the design (it would destroy visitor identity).
3. The same customDomains claimed on two frps servers route independently,
   each stamping the true visitor address into the PROXY v2 header (k=2
   multi-homing works).
4. An ``httpPlugins`` ``addr`` carrying URL userinfo reaches the plugin
   endpoint as an ``Authorization: Basic`` header with a secret-free path --
   what keeps the connector-auth secret out of access logs (issue #616).

NOT a pytest test: it needs the network (a GitHub release download) and free
loopback ports; it exists to be run by an operator or agent when bumping frp.
"""

import base64
import hashlib
import http.server
import json
import platform
import queue
import socket
import socketserver
import ssl
import struct
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Final

import httpx
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import RunningProcess
from imbue.imbue_common.logging import setup_logging
from imbue.share_relay.errors import ShareRelayError
from imbue.share_relay.remote_install import FRP_VERSION
from imbue.share_relay.remote_install import pinned_frp_release

# Loopback port layout for the harness (globally unique-ish to avoid clashes).
_FRPS1_BIND_PORT: Final[int] = 47100
_FRPS1_VHOST_PORT: Final[int] = 47143
_FRPS2_BIND_PORT: Final[int] = 47200
_FRPS2_VHOST_PORT: Final[int] = 47243
_FRPC1_ADMIN_PORT: Final[int] = 47301
_FRPC2_ADMIN_PORT: Final[int] = 47302
_BACKEND_PORT: Final[int] = 47443
_FRPS3_BIND_PORT: Final[int] = 47500
_FRPS3_VHOST_PORT: Final[int] = 47543
_PLUGIN_SERVER_PORT: Final[int] = 47501

_TUNNEL_UP_ATTEMPTS: Final[int] = 40
_SPLICE_WAIT_SECONDS: Final[float] = 0.5
_DOWNLOAD_TIMEOUT_SECONDS: Final[float] = 60.0

_PROXY_V2_SIGNATURE: Final[bytes] = b"\r\n\r\n\x00\r\nQUIT\n"

# TLS alert record for fatal (2) unrecognized_name (112): what frps answers an
# unknown SNI with (pkg/util/vhost/https.go vhostFailed).
_UNRECOGNIZED_NAME_ALERT: Final[bytes] = bytes.fromhex("15030300020270")

_TEST_DOMAIN: Final[str] = "web.host-verify.user.us1.frp-verify.invalid"

# Fixtures for the plugin-userinfo check: the shape render_frps_toml produces
# (secret as the addr's userinfo username, relay id as the final path segment).
_PLUGIN_TEST_DOMAIN: Final[str] = "web.host-plugin.user.us1.frp-verify.invalid"
_PLUGIN_AUTH_SECRET: Final[str] = "a3f1c9d7e5b24680deadbeef00112233"
_PLUGIN_RELAY_ID: Final[str] = "relay-abcdef0123456789"
_PLUGIN_AUTH_PATH: Final[str] = f"/frps/auth/{_PLUGIN_RELAY_ID}"
_PLUGIN_CALLBACK_WAIT_SECONDS: Final[float] = 30.0


class FrpVerificationError(ShareRelayError):
    """Raised when a verified-behavior assertion fails (the frp bump changed semantics)."""


# Each spliced connection's PROXY v2 source, pushed by the backend handler and
# consumed (as a blocking condition wait) by the checks below.
_SPLICED_SOURCES: Final["queue.Queue[tuple[str, int]]"] = queue.Queue()


class _SpliceRecordingHandler(socketserver.BaseRequestHandler):
    """Backend for the frpc proxies: records each connection's PROXY v2 source, then holds it open."""

    def handle(self) -> None:
        try:
            data = self.request.recv(65536)
        except OSError:
            return
        if data.startswith(_PROXY_V2_SIGNATURE) and len(data) >= 16 and data[13] == 0x11:
            body = data[16:]
            source_ip = socket.inet_ntoa(body[0:4])
            (source_port,) = struct.unpack(">H", body[8:10])
            _SPLICED_SOURCES.put((source_ip, source_port))
        # Hold the splice open; never answer the TLS handshake.
        try:
            while self.request.recv(65536):
                pass
        except OSError:
            pass


class _BackendServer(socketserver.ThreadingTCPServer):
    """Loopback backend server; daemon threads so teardown never hangs on held-open splices."""

    allow_reuse_address = True
    daemon_threads = True


def _download_pinned_frp(work_dir: Path) -> Path:
    # The harness downloads and runs the *linux* frps/frpc binaries (matching
    # what deploys to relay hosts), so it can only run on a Linux host.
    if platform.system() != "Linux":
        raise FrpVerificationError(
            f"The frp verification harness runs Linux frps/frpc binaries and must be run on a Linux host "
            f"(e.g. a dev workspace), not {platform.system()}"
        )
    machine = platform.machine()
    goarch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine)
    if goarch is None:
        raise FrpVerificationError(f"Unsupported architecture for the harness: {machine}")
    url, expected_sha256 = pinned_frp_release(goarch)
    tarball_path = work_dir / "frp.tar.gz"
    # GitHub release downloads redirect to the objects CDN, so redirects must
    # be followed explicitly.
    response = httpx.get(url, follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()
    tarball_path.write_bytes(response.content)
    actual_sha256 = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
    if actual_sha256 != expected_sha256:
        raise FrpVerificationError(f"frp tarball sha256 mismatch: expected {expected_sha256}, got {actual_sha256}")
    with tarfile.open(tarball_path) as tarball:
        tarball.extractall(work_dir, filter="data")
    return work_dir / f"frp_{FRP_VERSION}_linux_{goarch}"


def _build_client_hello(sni: str) -> bytes:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls_object = context.wrap_bio(incoming, outgoing, server_hostname=sni)
    try:
        tls_object.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def _send_hello(port: int, sni: str, prefix: bytes) -> tuple[str, bytes]:
    """Send (optionally prefixed) ClientHello; returns (outcome, first_bytes) where outcome is DATA/FIN/RST/HANG."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(prefix + _build_client_hello(sni))
        connection.settimeout(5)
        try:
            data = connection.recv(4096)
        except ConnectionResetError:
            return ("RST", b"")
        except TimeoutError:
            return ("HANG", b"")
        return ("FIN", b"") if data == b"" else ("DATA", data)


def _splice_one_visitor(vhost_port: int, wait_seconds: float) -> tuple[int, int]:
    """Splice one visitor connection through frps; returns (visitor local port, PROXY-header source port).

    Raises :class:`queue.Empty` when no splice reached the backend in time
    (the tunnel is not up yet, or the connection was refused/misrouted).
    """
    with socket.create_connection(("127.0.0.1", vhost_port), timeout=5) as connection:
        local_port = connection.getsockname()[1]
        connection.sendall(_build_client_hello(_TEST_DOMAIN))
        _source_ip, source_port = _SPLICED_SOURCES.get(timeout=wait_seconds)
    return (local_port, source_port)


def _wait_for_tunnel(vhost_port: int) -> None:
    """Poll until a visitor connection splices through to the backend.

    A successful-but-empty attempt is paced by the blocking splice wait (a
    real condition wait on the backend receiving the connection); a refused
    connection (frps still binding its vhost listener) fails instantly, so
    that path is paced by the poll interval -- the poll-with-timeout pattern
    the time.sleep ratchet prescribes (see mngr's utils/polling.py).
    """
    for _attempt in range(_TUNNEL_UP_ATTEMPTS):
        try:
            _splice_one_visitor(vhost_port, _SPLICE_WAIT_SECONDS)
            return
        except queue.Empty:
            continue
        except OSError:
            time.sleep(_SPLICE_WAIT_SECONDS)
    raise FrpVerificationError(f"No tunnel spliced through vhost port {vhost_port} in time")


def _frps_toml(bind_port: int, vhost_port: int) -> str:
    return f"bindPort = {bind_port}\nvhostHTTPSPort = {vhost_port}\n"


# Each plugin callback the capture server received: (op, path, authorization
# header). Pushed by the handler, consumed by the plugin-userinfo check.
_PLUGIN_CALLBACKS: Final["queue.Queue[tuple[str, str, str]]"] = queue.Queue()


class _PluginCaptureHandler(http.server.BaseHTTPRequestHandler):
    """Stand-in connector: records each plugin callback's path + Authorization header and allows the op."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        # A non-JSON body means frp changed its plugin payload -- let the
        # handler raise (http.server prints the traceback) and the check time
        # out rather than recording a half-parsed callback.
        op = str(json.loads(body).get("op", ""))
        _PLUGIN_CALLBACKS.put((op, self.path, self.headers.get("Authorization", "")))
        answer = json.dumps({"reject": False, "unchange": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _frps_plugin_toml(bind_port: int, vhost_port: int) -> str:
    # The plugin server is plain http on loopback: Go's HTTP client applies URL
    # userinfo as the Authorization header after parsing the URL, independent
    # of scheme, so http here pins the same behavior the https production addr
    # relies on without cert plumbing.
    return f"""\
bindPort = {bind_port}
vhostHTTPSPort = {vhost_port}

[[httpPlugins]]
name = "connector-auth"
addr = "http://{_PLUGIN_AUTH_SECRET}@127.0.0.1:{_PLUGIN_SERVER_PORT}"
path = "{_PLUGIN_AUTH_PATH}"
ops = ["Login", "NewProxy", "Ping"]
"""


def _frpc_plugin_toml(server_port: int) -> str:
    return f"""\
serverAddr = "127.0.0.1"
serverPort = {server_port}
loginFailExit = false
transport.tls.enable = true
metadatas.relay_token = "frp-verify-relay-token"

[[proxies]]
name = "share"
type = "https"
localIP = "127.0.0.1"
localPort = {_BACKEND_PORT}
customDomains = ["{_PLUGIN_TEST_DOMAIN}"]
"""


def _frpc_toml(server_port: int, admin_port: int) -> str:
    return f"""\
serverAddr = "127.0.0.1"
serverPort = {server_port}
loginFailExit = false
transport.tls.enable = true
webServer.addr = "127.0.0.1"
webServer.port = {admin_port}

[[proxies]]
name = "share"
type = "https"
localIP = "127.0.0.1"
localPort = {_BACKEND_PORT}
customDomains = ["{_TEST_DOMAIN}"]
transport.proxyProtocolVersion = "v2"
"""


def _start_frp_process(
    concurrency_group: ConcurrencyGroup, binaries_dir: Path, work_dir: Path, config_name: str, content: str
) -> RunningProcess:
    config_path = work_dir / config_name
    config_path.write_text(content)
    binary = "frps" if config_name.startswith("frps") else "frpc"
    # Terminated explicitly at the end of the run (SIGTERM makes frp exit
    # non-zero, which is expected, hence is_checked_by_group=False).
    return concurrency_group.run_process_in_background(
        [str(binaries_dir / binary), "-c", str(config_path)],
        name=f"frp-verify-{config_name}",
        is_checked_by_group=False,
    )


def _check(label: str, is_ok: bool, detail: str) -> None:
    if not is_ok:
        raise FrpVerificationError(f"FAIL {label}: {detail}")
    logger.info("PASS {}: {}", label, detail)


def _check_unknown_sni(vhost_port: int) -> None:
    outcome, data = _send_hello(vhost_port, "nobody.claimed.this.invalid", b"")
    _check(
        "unknown-SNI alert",
        outcome == "DATA" and data == _UNRECOGNIZED_NAME_ALERT,
        f"outcome={outcome} bytes={data.hex()}",
    )


def _check_inbound_proxy_rejected(vhost_port: int) -> None:
    proxy_body = socket.inet_aton("10.9.8.7") + socket.inet_aton("127.0.0.1") + struct.pack(">HH", 33333, 443)
    proxy_prefix = _PROXY_V2_SIGNATURE + bytes([0x21, 0x11]) + struct.pack(">H", len(proxy_body)) + proxy_body
    outcome, data = _send_hello(vhost_port, _TEST_DOMAIN, prefix=proxy_prefix)
    _check("inbound-PROXY rejected", outcome in ("RST", "FIN"), f"outcome={outcome} bytes={data.hex()}")


def _drain_spliced_sources() -> None:
    """Discard queued splice records from earlier connections.

    A `_wait_for_tunnel` attempt that timed out can have its splice land just
    after the wait, leaving a stale item queued; consuming it would pair a new
    connection's local port with the old connection's PROXY-header port.
    """
    for _stale in range(_SPLICED_SOURCES.qsize()):
        try:
            _SPLICED_SOURCES.get_nowait()
        except queue.Empty:
            return


def _check_visitor_identity(label: str, vhost_port: int) -> None:
    _drain_spliced_sources()
    try:
        visitor_port, header_port = _splice_one_visitor(vhost_port, 5.0)
    except queue.Empty as exc:
        raise FrpVerificationError(f"FAIL dual-claim via {label}: splice never reached the backend") from exc
    _check(
        f"dual-claim visitor identity via {label}",
        visitor_port == header_port,
        f"visitor_port={visitor_port} proxy_header_port={header_port}",
    )


def _check_plugin_userinfo_auth(concurrency_group: ConcurrencyGroup, binaries_dir: Path, work_dir: Path) -> None:
    """Behavior 4: plugin ``addr`` userinfo arrives as ``Authorization: Basic`` with a secret-free path.

    Runs a third frps whose ``httpPlugins`` addr embeds the secret as URL
    userinfo (the exact shape ``render_frps_toml`` produces) against a local
    stand-in connector, drives one frpc through ``Login`` + ``NewProxy``, and
    asserts every callback carried the expected Basic header while the URL
    path stayed secret-free. frp appends its own ``?op=...&version=...`` query
    to the plugin URL, so the path is compared without its query string.
    """
    plugin_server = http.server.ThreadingHTTPServer(("127.0.0.1", _PLUGIN_SERVER_PORT), _PluginCaptureHandler)
    plugin_thread = threading.Thread(target=plugin_server.serve_forever, name="frp-verify-plugin", daemon=True)
    plugin_thread.start()
    frp_processes = [
        _start_frp_process(concurrency_group, binaries_dir, work_dir, config_name, content)
        for config_name, content in (
            ("frps3.toml", _frps_plugin_toml(_FRPS3_BIND_PORT, _FRPS3_VHOST_PORT)),
            ("frpc3.toml", _frpc_plugin_toml(_FRPS3_BIND_PORT)),
        )
    ]
    try:
        # Collect callbacks until Login and NewProxy have both been observed
        # (frpc sends Login immediately and NewProxy right after it succeeds).
        callbacks: list[tuple[str, str, str]] = []
        deadline = time.monotonic() + _PLUGIN_CALLBACK_WAIT_SECONDS
        while {"Login", "NewProxy"} - {op for op, _path, _auth in callbacks}:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FrpVerificationError(
                    f"FAIL plugin-userinfo auth: expected Login+NewProxy callbacks, "
                    f"saw {sorted({op for op, _path, _auth in callbacks})}"
                )
            try:
                callbacks.append(_PLUGIN_CALLBACKS.get(timeout=min(remaining, _SPLICE_WAIT_SECONDS)))
            except queue.Empty:
                continue
    finally:
        for frp_process in frp_processes:
            frp_process.terminate()
        plugin_server.shutdown()
        plugin_server.server_close()

    expected_authorization = "Basic " + base64.b64encode(f"{_PLUGIN_AUTH_SECRET}:".encode()).decode()
    for op, path, authorization in callbacks:
        _check(
            f"plugin-userinfo Authorization header on {op}",
            authorization == expected_authorization,
            f"Authorization={authorization!r}",
        )
        _check(
            f"plugin-userinfo secret-free path on {op}",
            path.split("?", 1)[0] == _PLUGIN_AUTH_PATH and _PLUGIN_AUTH_SECRET not in path,
            f"path={path!r}",
        )


def run_frp_verification(work_dir: Path) -> None:
    """Download the pinned frp and assert the multi-relay-critical behaviors on loopback."""
    binaries_dir = _download_pinned_frp(work_dir)
    backend = _BackendServer(("127.0.0.1", _BACKEND_PORT), _SpliceRecordingHandler)
    backend_thread = threading.Thread(target=backend.serve_forever, name="frp-verify-backend", daemon=True)
    backend_thread.start()
    try:
        with ConcurrencyGroup(name="frp-verification") as concurrency_group:
            # Two frps servers plus one frpc per server, all claiming the SAME
            # domain (the k=2 multi-homing shape).
            frp_processes = [
                _start_frp_process(concurrency_group, binaries_dir, work_dir, config_name, content)
                for config_name, content in (
                    ("frps1.toml", _frps_toml(_FRPS1_BIND_PORT, _FRPS1_VHOST_PORT)),
                    ("frps2.toml", _frps_toml(_FRPS2_BIND_PORT, _FRPS2_VHOST_PORT)),
                    ("frpc1.toml", _frpc_toml(_FRPS1_BIND_PORT, _FRPC1_ADMIN_PORT)),
                    ("frpc2.toml", _frpc_toml(_FRPS2_BIND_PORT, _FRPC2_ADMIN_PORT)),
                )
            ]
            try:
                _wait_for_tunnel(_FRPS1_VHOST_PORT)
                _wait_for_tunnel(_FRPS2_VHOST_PORT)

                _check_unknown_sni(_FRPS1_VHOST_PORT)
                _check_inbound_proxy_rejected(_FRPS1_VHOST_PORT)
                _check_visitor_identity("server-1", _FRPS1_VHOST_PORT)
                _check_visitor_identity("server-2", _FRPS2_VHOST_PORT)
                _check_plugin_userinfo_auth(concurrency_group, binaries_dir, work_dir)
            finally:
                for frp_process in frp_processes:
                    frp_process.terminate()
        logger.info("All frp {} behavior checks passed.", FRP_VERSION)
    finally:
        backend.shutdown()
        backend.server_close()


def main() -> None:
    setup_logging(level="INFO")
    with tempfile.TemporaryDirectory(prefix="frp-verify-") as temp_dir:
        run_frp_verification(Path(temp_dir))


if __name__ == "__main__":
    main()
