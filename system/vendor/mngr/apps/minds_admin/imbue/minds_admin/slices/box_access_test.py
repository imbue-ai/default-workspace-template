import socket
import threading
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.minds.config.data_types import ManagementPlaneConfig
from imbue.minds_admin.slices.box_access import BoxManagementDial
from imbue.minds_admin.slices.box_access import OperatorWireguardIdentity
from imbue.minds_admin.slices.box_access import _resolve_single_flight
from imbue.minds_admin.slices.box_access import _spawn_verified_tunnel_or_none
from imbue.minds_admin.slices.box_access import build_onetun_command
from imbue.minds_admin.slices.box_access import choose_box_management_address
from imbue.minds_admin.slices.box_access import close_box_management_tunnels
from imbue.minds_admin.slices.box_access import derive_wireguard_public_key
from imbue.minds_admin.slices.box_access import is_tcp_port_reachable
from imbue.minds_admin.slices.box_access import match_operator_identity_or_none
from imbue.minds_admin.slices.box_access import probe_ssh_banner
from imbue.minds_admin.slices.box_access import resolve_box_management_dial


def test_choose_box_management_address_prefers_a_reachable_overlay_address() -> None:
    chosen = choose_box_management_address(
        public_address="203.0.113.10", wireguard_address="10.202.1.1", is_wireguard_reachable=True
    )
    assert chosen == "10.202.1.1"


def test_choose_box_management_address_falls_back_when_the_overlay_is_unreachable() -> None:
    chosen = choose_box_management_address(
        public_address="203.0.113.10", wireguard_address="10.202.1.1", is_wireguard_reachable=False
    )
    assert chosen == "203.0.113.10"


def test_choose_box_management_address_falls_back_without_an_overlay_identity() -> None:
    # A first-ever prep: the box has no overlay address yet (and no lockdown
    # either), so the public address is the right dial.
    chosen = choose_box_management_address(
        public_address="203.0.113.10", wireguard_address=None, is_wireguard_reachable=False
    )
    assert chosen == "203.0.113.10"


def test_is_tcp_port_reachable_connects_to_a_live_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        # No accept() needed: the kernel completes the handshake from the
        # listen backlog, which is all the probe's connect observes.
        listener.listen(1)
        _address, port = listener.getsockname()

        assert is_tcp_port_reachable("127.0.0.1", port, 1.0) is True


def test_is_tcp_port_reachable_reports_a_closed_port_as_unreachable() -> None:
    # Bind (reserving the port) without listening, so a connect is refused.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        _address, port = reserved.getsockname()

    assert is_tcp_port_reachable("127.0.0.1", port, 1.0) is False


# A fixed X25519 pair, cross-checked once against `wg pubkey` (the raw private
# key bytes 0x01..0x20): the derivation must match WireGuard's own exactly.
_PRIVATE_KEY_BASE64 = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="
_PUBLIC_KEY_BASE64 = "B6N8vBQgk8i3VdwbEOhstCY3StFqqFPtC9/AsrhtHHw="


def _management_plane_config(operator_public_key: str) -> ManagementPlaneConfig:
    return ManagementPlaneConfig.model_validate(
        {
            "wireguard": {
                "listen_port": 51820,
                "operators": [{"name": "josh", "public_key": operator_public_key, "address": "10.112.0.2"}],
            }
        }
    )


def test_derive_wireguard_public_key_matches_wg_pubkey() -> None:
    assert derive_wireguard_public_key(_PRIVATE_KEY_BASE64) == _PUBLIC_KEY_BASE64


def test_derive_wireguard_public_key_rejects_non_key_material() -> None:
    with pytest.raises(ValueError):
        derive_wireguard_public_key("not base64!!!")


def test_match_operator_identity_finds_the_committed_operator_for_the_local_key(tmp_path: Path) -> None:
    key_path = tmp_path / "dev.key"
    key_path.write_text(_PRIVATE_KEY_BASE64 + "\n")

    identity = match_operator_identity_or_none(
        _management_plane_config(_PUBLIC_KEY_BASE64), key_path, key_path.read_text()
    )

    assert identity is not None
    assert identity.source_address == "10.112.0.2"
    assert identity.listen_port == 51820
    assert identity.private_key_path == key_path


def test_match_operator_identity_returns_none_for_an_uncommitted_key(tmp_path: Path) -> None:
    key_path = tmp_path / "dev.key"
    key_path.write_text(_PRIVATE_KEY_BASE64 + "\n")

    identity = match_operator_identity_or_none(
        _management_plane_config("K3qb0S0GaUdCVv6q9U0S7Wl2v7q1B1S6q6S8q1S6q1o="), key_path, key_path.read_text()
    )

    assert identity is None


def test_match_operator_identity_returns_none_for_malformed_key_material(tmp_path: Path) -> None:
    key_path = tmp_path / "dev.key"
    key_path.write_text("definitely not a key\n")

    identity = match_operator_identity_or_none(
        _management_plane_config(_PUBLIC_KEY_BASE64), key_path, key_path.read_text()
    )

    assert identity is None


def test_build_onetun_command_forwards_local_to_the_overlay_ssh_and_keeps_the_key_out_of_argv() -> None:
    command = build_onetun_command(
        onetun_path="/opt/onetun",
        local_port=45123,
        box_overlay_address="10.112.1.1",
        endpoint_address="51.81.208.81",
        endpoint_port=51820,
        box_public_key="boxpub=",
        source_address="10.112.0.2",
    )

    assert command == snapshot(
        [
            "/opt/onetun",
            "127.0.0.1:45123:10.112.1.1:22:TCP",
            "--endpoint-addr",
            "51.81.208.81:51820",
            "--endpoint-public-key",
            "boxpub=",
            "--source-peer-ip",
            "10.112.0.2",
            "--keep-alive",
            "25",
        ]
    )
    # The private key must never appear in the argv (visible in the process
    # table); the spawner passes it via ONETUN_PRIVATE_KEY.
    assert not any("PRIVATE" in argument or "AQID" in argument for argument in command)


def _accept_and_send(listener: socket.socket, banner: bytes) -> None:
    connection, _peer = listener.accept()
    connection.sendall(banner)
    connection.close()


def _serve_banner_once(banner: bytes) -> tuple[socket.socket, int]:
    """A listener that answers its first connection with ``banner``; returns (socket, port)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    _address, port = listener.getsockname()
    threading.Thread(target=lambda: _accept_and_send(listener, banner), daemon=True).start()
    return listener, port


def test_probe_ssh_banner_accepts_an_ssh_endpoint() -> None:
    listener, port = _serve_banner_once(b"SSH-2.0-OpenSSH_9.2\r\n")
    try:
        assert probe_ssh_banner("127.0.0.1", port, 2.0) is True
    finally:
        listener.close()


def test_probe_ssh_banner_rejects_a_non_ssh_endpoint() -> None:
    listener, port = _serve_banner_once(b"HTTP/1.1 400 Bad Request\r\n")
    try:
        assert probe_ssh_banner("127.0.0.1", port, 2.0) is False
    finally:
        listener.close()


_STUB_ONETUN = """\
#!/usr/bin/env python3
import os
import socket
import sys

assert os.environ.get("ONETUN_PRIVATE_KEY"), "the spawner must pass the key via ONETUN_PRIVATE_KEY"
local_port = int(sys.argv[1].split(":")[1])
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", local_port))
listener.listen(4)
# Bounded on purpose: a stub only ever needs to answer the test's own probes.
for _ in range(64):
    connection, _peer = listener.accept()
    connection.sendall({banner!r})
    connection.close()
"""


def _write_stub_onetun(tmp_path: Path, banner: bytes) -> Path:
    stub_path = tmp_path / "onetun"
    stub_path.write_text(_STUB_ONETUN.format(banner=banner))
    stub_path.chmod(0o755)
    return stub_path


def _identity(tmp_path: Path) -> OperatorWireguardIdentity:
    key_path = tmp_path / "dev.key"
    key_path.write_text(_PRIVATE_KEY_BASE64 + "\n")
    return OperatorWireguardIdentity(private_key_path=key_path, source_address="10.112.0.2", listen_port=51820)


def test_spawn_verified_tunnel_yields_a_local_dial_when_the_banner_arrives(tmp_path: Path) -> None:
    # A stub onetun binds the requested local forward and answers with an SSH
    # banner, standing in for a real tunnel to a box sshd.
    stub_path = _write_stub_onetun(tmp_path, b"SSH-2.0-stub\r\n")

    try:
        dial = _spawn_verified_tunnel_or_none(
            onetun_path=str(stub_path),
            identity=_identity(tmp_path),
            box_overlay_address="10.112.1.1",
            box_public_key="boxpub=",
            endpoint_address="203.0.113.10",
        )

        assert dial is not None
        assert dial.host == "127.0.0.1"
        # The dial's port must answer with the banner end to end.
        assert probe_ssh_banner(dial.host, dial.port, 2.0) is True
    finally:
        # A verified tunnel deliberately lives until interpreter exit; the
        # test must not leak its stub process past the session.
        close_box_management_tunnels()
    assert is_tcp_port_reachable("127.0.0.1", dial.port, 0.5) is False


def test_spawn_verified_tunnel_reaps_and_returns_none_without_an_ssh_banner(tmp_path: Path) -> None:
    stub_path = _write_stub_onetun(tmp_path, b"NOT-SSH\r\n")

    try:
        dial = _spawn_verified_tunnel_or_none(
            onetun_path=str(stub_path),
            identity=_identity(tmp_path),
            box_overlay_address="10.112.1.1",
            box_public_key="boxpub=",
            endpoint_address="203.0.113.10",
        )
    finally:
        close_box_management_tunnels()

    assert dial is None


def test_spawn_verified_tunnel_returns_none_when_the_tunnel_process_dies(tmp_path: Path) -> None:
    stub_path = tmp_path / "onetun"
    stub_path.write_text("#!/usr/bin/env python3\nraise SystemExit(3)\n")
    stub_path.chmod(0o755)

    try:
        dial = _spawn_verified_tunnel_or_none(
            onetun_path=str(stub_path),
            identity=_identity(tmp_path),
            box_overlay_address="10.112.1.1",
            box_public_key="boxpub=",
            endpoint_address="203.0.113.10",
        )
    finally:
        close_box_management_tunnels()

    assert dial is None


def test_resolve_single_flight_resolves_a_contended_key_exactly_once() -> None:
    # Eight destroy threads dialing the same box at once must share ONE resolution
    # (one tunnel): the box's wg0 keeps a single session per operator peer, so a
    # tunnel per thread would drop each other's SSH sessions.
    cache: dict[tuple[str, str | None, str | None], BoxManagementDial] = {}
    lock = threading.Lock()
    key = ("203.0.113.10", "10.112.1.1", "boxpub=")
    resolve_started = threading.Event()
    release_resolver = threading.Event()
    resolve_count = 0
    resolve_count_lock = threading.Lock()

    def slow_resolve() -> BoxManagementDial:
        nonlocal resolve_count
        with resolve_count_lock:
            resolve_count += 1
        resolve_started.set()
        assert release_resolver.wait(timeout=5.0)
        return BoxManagementDial(host="127.0.0.1", port=43127)

    results: list[BoxManagementDial] = []
    results_lock = threading.Lock()

    def worker() -> None:
        dial = _resolve_single_flight(cache, lock, key, slow_resolve)
        with results_lock:
            results.append(dial)

    threads = [threading.Thread(target=worker, name=f"dial-{idx}") for idx in range(8)]
    for thread in threads:
        thread.start()
    # Every thread is queued behind the first resolution before it completes.
    assert resolve_started.wait(timeout=5.0)
    release_resolver.set()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive()

    assert resolve_count == 1
    assert results == [BoxManagementDial(host="127.0.0.1", port=43127)] * 8
    assert cache == {key: BoxManagementDial(host="127.0.0.1", port=43127)}


def test_resolve_box_management_dial_without_an_overlay_identity_is_the_public_ssh_port() -> None:
    try:
        dial = resolve_box_management_dial(
            public_address="203.0.113.77", wireguard_address=None, wireguard_public_key=None
        )
    finally:
        close_box_management_tunnels()
    assert dial == BoxManagementDial(host="203.0.113.77", port=22)
