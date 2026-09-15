"""Resolving the dial operator tooling uses for a box's management SSH (``:22``).

Once a gen-2 box's ``:22`` lockdown is live (specs/slice-fleet-gen2 phase 3),
its public address answers management SSH only from the connector's Modal
Proxy egress and the tier's WireGuard overlay -- an operator machine's direct
dial is dropped. Every operator-side box ``:22`` call site therefore resolves
a :class:`BoxManagementDial` (an address + port) here, trying in order:

1. **A userspace WireGuard tunnel** (`onetun <https://github.com/aramperes/onetun>`_):
   when the ``onetun`` binary and the operator's WireGuard private key are
   available and the box has a recorded overlay identity, a per-box local TCP
   forward is spawned that speaks the WireGuard protocol entirely in-process
   -- no root, no network interface, no local peer list (the peer is built
   from the box's DB row per dial). The dial is then ``127.0.0.1:<port>``,
   verified end to end by reading the box sshd's banner through the tunnel.
2. **A kernel-route overlay dial**: the box's overlay address directly, when
   a fast TCP probe shows an interface-level tunnel (``wg-quick``) reaches it.
3. **The public address** -- correct for every box without a live lockdown.

Automatic -- there is no flag to forget: a first-ever prep (no overlay
identity yet, but no lockdown either) and a machine without tunnel material
both fall back to the public address after at most one fast probe.

Only management SSH moves onto a tunnel. The per-slice ports (22000+) are not
locked down and their user-facing records keep the public address; box
host-key pinning is by key, not address, so the dial is transparent to trust.
"""

import atexit
import base64
import binascii
import os
import shutil
import socket
import threading
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.local_process import RunningProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.config.data_types import ManagementPlaneConfig
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.paths import active_env_name_or_none
from imbue.minds_admin.slices.onetun_install import well_known_onetun_path
from imbue.minds_admin.slices.operator_identity import OPERATOR_IDENTITY_DIR_ENV_VAR
from imbue.minds_admin.slices.operator_identity import operator_wireguard_key_path
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import tier_for_env_name

MANAGEMENT_SSH_PORT: Final[int] = 22

# Overlay round trips are tens of milliseconds (same-continent WireGuard), so
# one second is ample headroom -- and an operator without the tunnel up must
# not stall every command noticeably before the public-address fallback.
_OVERLAY_PROBE_TIMEOUT_SECONDS: Final[float] = 1.0

# The userspace tunnel's end-to-end verification (WireGuard handshake plus the
# box sshd's banner) pays a few round trips; paid once per box per process,
# only when a tunnel candidate exists. The banner wait is polled (the tunnel
# process needs a moment to bind its local listener), each attempt bounded so
# a broken-but-listening tunnel cannot stall past the overall deadline.
_TUNNEL_BANNER_ATTEMPT_TIMEOUT_SECONDS: Final[float] = 2.0
_TUNNEL_VERIFY_TIMEOUT_SECONDS: Final[float] = 8.0


class BoxManagementDial(FrozenModel):
    """Where operator tooling dials a box's management SSH (``127.0.0.1:<port>`` when tunneled)."""

    host: str = Field(description="Address to dial (a local forward, the overlay address, or the public one).")
    port: int = Field(description="Port to dial (a tunnel's local forward port, else 22).")


class OperatorWireguardIdentity(FrozenModel):
    """The local operator's tunnel material: their committed peer identity plus the on-disk key."""

    private_key_path: Path = Field(description="The operator's WireGuard private key file (never leaves disk).")
    source_address: str = Field(description="The operator's committed overlay address (the tunnel's source IP).")
    listen_port: int = Field(description="The tier's WireGuard listen port (the boxes' endpoint port).")


def is_tcp_port_reachable(address: str, port: int, timeout_seconds: float) -> bool:
    """One TCP dial; True when the connection completes within the timeout."""
    try:
        with socket.create_connection((address, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def probe_ssh_banner(host: str, port: int, timeout_seconds: float) -> bool:
    """True when the endpoint answers with an SSH banner -- an end-to-end liveness check.

    A userspace tunnel's local port accepts immediately (the forward is
    lazy), so a bare connect proves nothing; the banner proves the WireGuard
    handshake completed and the box's sshd answered through it.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as connection:
            connection.settimeout(timeout_seconds)
            return connection.recv(64).startswith(b"SSH-")
    except OSError:
        return False


@pure
def choose_box_management_address(
    *, public_address: str, wireguard_address: str | None, is_wireguard_reachable: bool
) -> str:
    if wireguard_address and is_wireguard_reachable:
        return wireguard_address
    return public_address


@pure
def derive_wireguard_public_key(private_key_base64: str) -> str:
    """The WireGuard public key for a private key (both in the base64 form ``wg`` uses).

    Raises ValueError on malformed key material. X25519 clamping is applied by
    the scalar multiplication itself, so this matches ``wg pubkey`` exactly.
    """
    raw = base64.b64decode(private_key_base64.strip(), validate=True)
    public_bytes = (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return base64.b64encode(public_bytes).decode()


def match_operator_identity_or_none(
    management_plane_config: ManagementPlaneConfig,
    private_key_path: Path,
    private_key_base64: str,
) -> OperatorWireguardIdentity | None:
    """The committed operator entry whose public key matches the local private key, or None."""
    try:
        local_public_key = derive_wireguard_public_key(private_key_base64)
    except (ValueError, binascii.Error):
        logger.warning("Failed to parse the WireGuard private key at {} as base64 key material", private_key_path)
        return None
    for operator in management_plane_config.wireguard.operators:
        if str(operator.public_key) == local_public_key:
            return OperatorWireguardIdentity(
                private_key_path=private_key_path,
                source_address=str(operator.address),
                listen_port=int(management_plane_config.wireguard.listen_port),
            )
    logger.warning(
        "Found a WireGuard private key at {} but no committed operator in the tier's [management_plane] table "
        "matches its public key {}; the userspace tunnel path is unavailable",
        private_key_path,
        local_public_key,
    )
    return None


@lru_cache(maxsize=None)
def _operator_identity_or_none() -> OperatorWireguardIdentity | None:
    """The local operator's tunnel material, resolved once per process (None = no userspace path).

    The private key lives in the operator's per-tier identity directory
    (``~/.mindsadmin/<tier>/wireguard.key``, see ``operator_identity``); its
    derived public key must match a committed
    ``[[management_plane.wireguard.operators]]`` entry of the activated
    tier's ``deploy.toml``.
    """
    env_name = active_env_name_or_none()
    if env_name is None:
        return None
    tier = tier_for_env_name(env_name)
    management_plane_config = load_deploy_config(tier).management_plane
    if management_plane_config is None or not management_plane_config.wireguard.operators:
        return None
    private_key_path = operator_wireguard_key_path(tier)
    if not private_key_path.is_file():
        # The tier has operator peers, so a box with a live :22 lockdown is
        # reachable only over the overlay; without the key every command
        # falls back to the public dial and fails with a bare timeout.
        logger.warning(
            "Found no operator WireGuard private key at {}, so the userspace tunnel to locked-down gen-2 boxes is "
            "unavailable and only the public dial remains (place the key there, or point {} at the identity root)",
            private_key_path,
            OPERATOR_IDENTITY_DIR_ENV_VAR,
        )
        return None
    return match_operator_identity_or_none(management_plane_config, private_key_path, private_key_path.read_text())


@lru_cache(maxsize=None)
def _onetun_path_or_none() -> str | None:
    """The onetun binary (MNGR_ONETUN_PATH override, else PATH, else the pinned install), or None when absent.

    Absence is logged once per process: at warning level when the operator's
    WireGuard key is in place (they clearly intend to use the transport), else
    at debug (a machine without tunnel material is the normal fallback case).
    """
    override = os.environ.get("MNGR_ONETUN_PATH")
    if override:
        return override
    found = shutil.which("onetun")
    if found is not None:
        return found
    well_known = well_known_onetun_path()
    if well_known.is_file():
        return str(well_known)
    if _operator_identity_or_none() is not None:
        logger.warning(
            "Found the operator WireGuard key but no onetun binary, so box-management SSH cannot take "
            "the userspace tunnel path; run `uv run minds-admin wireguard install-onetun` to install "
            "the pinned release (or set MNGR_ONETUN_PATH)"
        )
    else:
        logger.debug(
            "Found no onetun binary; the userspace WireGuard tunnel path is unavailable "
            "(run `uv run minds-admin wireguard install-onetun` or set MNGR_ONETUN_PATH)"
        )
    return None


@pure
def build_onetun_command(
    *,
    onetun_path: str,
    local_port: int,
    box_overlay_address: str,
    endpoint_address: str,
    endpoint_port: int,
    box_public_key: str,
    source_address: str,
) -> list[str]:
    """The onetun argv forwarding ``127.0.0.1:<local_port>`` to the box's overlay ``:22``.

    The operator's private key is deliberately NOT in the argv (it would be
    visible in the process table); the spawner passes it via onetun's
    ``ONETUN_PRIVATE_KEY`` environment variable.
    """
    return [
        onetun_path,
        f"127.0.0.1:{local_port}:{box_overlay_address}:{MANAGEMENT_SSH_PORT}:TCP",
        "--endpoint-addr",
        f"{endpoint_address}:{endpoint_port}",
        "--endpoint-public-key",
        box_public_key,
        "--source-peer-ip",
        source_address,
        "--keep-alive",
        "25",
    ]


def _pick_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return probe_socket.getsockname()[1]


# Every tunnel process ever spawned (dead ones included): the group's exit
# WAITS for its strands, so a close must terminate them first, and only this
# module knows which processes are tunnels.
_SPAWNED_TUNNEL_PROCESSES: Final[list[RunningProcess]] = []


@lru_cache(maxsize=None)
def _tunnel_concurrency_group() -> ConcurrencyGroup:
    """The process-lifetime ConcurrencyGroup owning every spawned tunnel.

    Tunnels must outlive any single command (the memoized dial resolver hands
    them out for the rest of the process), so the group cannot live in a
    ``with`` block: it is entered here on first use and closed at interpreter
    exit (terminating any still-running tunnel processes).
    """
    concurrency_group = ConcurrencyGroup(name="box-management-tunnels").__enter__()
    atexit.register(close_box_management_tunnels)
    return concurrency_group


def close_box_management_tunnels() -> None:
    """Terminate every spawned tunnel and reset the dial memoization.

    The default lifecycle keeps tunnels until interpreter exit; callers that
    must not leak child processes past their own scope (tests, long-lived
    embedders) close them explicitly. Idempotent; later dials simply re-spawn.
    """
    if _tunnel_concurrency_group.cache_info().currsize:
        # Terminate before exiting the group: the group's exit waits for its
        # strands (long-running processes are the caller's to stop).
        for process in _SPAWNED_TUNNEL_PROCESSES:
            if process.poll() is None:
                process.terminate()
        _tunnel_concurrency_group().__exit__(None, None, None)
    _SPAWNED_TUNNEL_PROCESSES.clear()
    _tunnel_concurrency_group.cache_clear()
    with _DIAL_CACHE_LOCK:
        _DIAL_BY_BOX_KEY.clear()


def _check_tunnel_banner(process: RunningProcess, local_port: int) -> bool | None:
    if process.poll() is not None:
        return False
    if probe_ssh_banner("127.0.0.1", local_port, _TUNNEL_BANNER_ATTEMPT_TIMEOUT_SECONDS):
        return True
    return None


def _await_tunnel_banner(process: RunningProcess, local_port: int) -> bool:
    """Poll the freshly spawned tunnel's local forward until the box's SSH banner arrives (or it dies)."""
    banner_seen, _poll_count, _elapsed = poll_for_value(
        lambda: _check_tunnel_banner(process, local_port),
        timeout=_TUNNEL_VERIFY_TIMEOUT_SECONDS,
        poll_interval=0.25,
    )
    return bool(banner_seen)


def _spawn_verified_tunnel_or_none(
    *,
    onetun_path: str,
    identity: OperatorWireguardIdentity,
    box_overlay_address: str,
    box_public_key: str,
    endpoint_address: str,
) -> BoxManagementDial | None:
    """Spawn a per-box onetun forward and verify it end to end, or None (process reaped) on failure."""
    local_port = _pick_free_local_port()
    command = build_onetun_command(
        onetun_path=onetun_path,
        local_port=local_port,
        box_overlay_address=box_overlay_address,
        endpoint_address=endpoint_address,
        endpoint_port=identity.listen_port,
        box_public_key=box_public_key,
        source_address=identity.source_address,
    )
    spawn_env = {**os.environ, "ONETUN_PRIVATE_KEY": identity.private_key_path.read_text().strip()}
    process = _tunnel_concurrency_group().run_process_in_background(
        command,
        env=spawn_env,
        # Long-running by design and terminated (SIGTERM, a non-zero exit) by
        # us or the group's close -- its exit code is never a failure signal.
        is_checked_by_group=False,
        is_output_accumulated=False,
        name=f"onetun-{box_overlay_address}",
    )
    _SPAWNED_TUNNEL_PROCESSES.append(process)
    if _await_tunnel_banner(process, local_port):
        logger.debug(
            "Opened a userspace WireGuard tunnel to box {} ({}): dialing 127.0.0.1:{}",
            box_overlay_address,
            endpoint_address,
            local_port,
        )
        return BoxManagementDial(host="127.0.0.1", port=local_port)
    logger.debug(
        "Failed to verify a userspace tunnel to box {} via {} (no SSH banner); falling back",
        box_overlay_address,
        endpoint_address,
    )
    process.terminate()
    return None


# Memoized per process under a single-flight lock: a multi-slice bake, destroy
# or drain dials the same box from many threads at once, and exactly one probe
# (or one tunnel) per box per command is what the box tolerates -- concurrent
# misses each spawning their own onetun would all present the same operator
# WireGuard identity, and the box's wg0 keeps one session per peer, so every
# fresh handshake would drop the other tunnels' SSH sessions. CLI invocations
# are short-lived, so reachability cannot meaningfully change mid-run.
_DIAL_CACHE_LOCK: Final[threading.Lock] = threading.Lock()
_DIAL_BY_BOX_KEY: Final[dict[tuple[str, str | None, str | None], BoxManagementDial]] = {}


def _resolve_single_flight(
    cache: dict[tuple[str, str | None, str | None], BoxManagementDial],
    lock: threading.Lock,
    key: tuple[str, str | None, str | None],
    resolve: Callable[[], BoxManagementDial],
) -> BoxManagementDial:
    """Return the cached dial for ``key``, computing it under ``lock`` so concurrent misses resolve exactly once."""
    with lock:
        cached = cache.get(key)
        if cached is not None:
            return cached
        resolved = resolve()
        cache[key] = resolved
        return resolved


def resolve_box_management_dial(
    *, public_address: str, wireguard_address: str | None, wireguard_public_key: str | None
) -> BoxManagementDial:
    """The dial for a box's management SSH: userspace tunnel, else overlay route, else public ``:22``."""
    return _resolve_single_flight(
        _DIAL_BY_BOX_KEY,
        _DIAL_CACHE_LOCK,
        (public_address, wireguard_address, wireguard_public_key),
        lambda: _resolve_box_management_dial_uncached(
            public_address=public_address,
            wireguard_address=wireguard_address,
            wireguard_public_key=wireguard_public_key,
        ),
    )


def _resolve_box_management_dial_uncached(
    *, public_address: str, wireguard_address: str | None, wireguard_public_key: str | None
) -> BoxManagementDial:
    if wireguard_address and wireguard_public_key:
        identity = _operator_identity_or_none()
        onetun_path = _onetun_path_or_none()
        if identity is not None and onetun_path is not None:
            tunneled = _spawn_verified_tunnel_or_none(
                onetun_path=onetun_path,
                identity=identity,
                box_overlay_address=wireguard_address,
                box_public_key=wireguard_public_key,
                endpoint_address=public_address,
            )
            if tunneled is not None:
                return tunneled
    address = resolve_box_management_address(public_address=public_address, wireguard_address=wireguard_address)
    return BoxManagementDial(host=address, port=MANAGEMENT_SSH_PORT)


@lru_cache(maxsize=None)
def resolve_box_management_address(*, public_address: str, wireguard_address: str | None) -> str:
    """The interface-route half of the ladder: a reachable overlay address, else the public one."""
    if not wireguard_address:
        return public_address
    is_wireguard_reachable = is_tcp_port_reachable(
        wireguard_address, MANAGEMENT_SSH_PORT, _OVERLAY_PROBE_TIMEOUT_SECONDS
    )
    chosen = choose_box_management_address(
        public_address=public_address,
        wireguard_address=wireguard_address,
        is_wireguard_reachable=is_wireguard_reachable,
    )
    if is_wireguard_reachable:
        logger.debug("Reached box management SSH over the WireGuard overlay at {}", wireguard_address)
    else:
        logger.debug(
            "Found overlay address {} unreachable; dialing the public address {}", wireguard_address, public_address
        )
    return chosen


def resolve_server_management_dial(server: BareMetalServer) -> BoxManagementDial:
    """The management SSH dial for a box row (the caller has checked public_address is set)."""
    return resolve_box_management_dial(
        public_address=str(server.public_address),
        wireguard_address=server.wireguard_address,
        wireguard_public_key=server.wireguard_public_key,
    )
