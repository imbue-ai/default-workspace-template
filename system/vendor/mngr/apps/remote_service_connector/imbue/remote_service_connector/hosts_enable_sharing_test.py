"""Tests for the server-driven enable-sharing endpoint and its composition helpers."""

import hashlib
import re
import socket
import threading
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Final
from uuid import UUID

import paramiko
import pytest

from imbue.remote_service_connector.hosts import _assert_container_serves_pinned_host_key
from imbue.remote_service_connector.hosts import build_owner_grants_toml
from imbue.remote_service_connector.hosts import build_share_env_text
from imbue.remote_service_connector.shares import derive_share_user_label
from imbue.remote_service_connector.testing import _CONTENT_DOMAIN
from imbue.remote_service_connector.testing import _USER_STUB_EMAIL
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _make_pool_quota_test_client
from imbue.remote_service_connector.testing import _user_headers

_HOST_DB_ID = UUID("00000000-0000-0000-0000-0000000000aa")
_HOST_ID_STR = "host-" + "a" * 32
_CHROME_ORIGIN = "https://minds.imbue.com"
# derive_share_user_label(_USER_STUB_USER_ID): the hyphen-stripped UUID.
_OWNER_LABEL = _USER_STUB_USER_ID.replace("-", "")


def _install_share_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARE_CONTENT_DOMAIN", _CONTENT_DOMAIN)
    monkeypatch.setenv("SHARE_CHROME_ORIGIN", _CHROME_ORIGIN)


def test_build_share_env_text_includes_chrome_origin_when_present() -> None:
    text = build_share_env_text(
        workspace_domain="host-x.user.us1.example",
        relay_token="tok",
        connector_url="https://c.example",
        broker_url="https://c.example",
        chrome_origin="https://minds.imbue.com",
    )
    assert "export SHARE_WORKSPACE_DOMAIN=host-x.user.us1.example" in text
    assert "export SHARE_RELAY_TOKEN=tok" in text
    assert "export SHARE_CHROME_ORIGIN=https://minds.imbue.com" in text


def test_build_share_env_text_omits_chrome_origin_when_empty() -> None:
    text = build_share_env_text(
        workspace_domain="d",
        relay_token="t",
        connector_url="u",
        broker_url="u",
        chrome_origin="",
    )
    assert "SHARE_CHROME_ORIGIN" not in text


def test_build_owner_grants_toml_seeds_the_owner_email() -> None:
    assert build_owner_grants_toml("owner@example.com") == (
        '[workspace]\nemails = ["owner@example.com"]\nemail_domains = []\n'
    )
    assert build_owner_grants_toml(None) == "[workspace]\nemails = []\nemail_domains = []\n"


def test_enable_sharing_creates_share_and_injects_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["host_id"] == _HOST_ID_STR
    # The test host has no datacenter record, so the region is the
    # deterministic hash-of-host-id spread: host-aaa... lands on us1.
    assert body["region"] == "us1"
    # Workspace-keyed share: the domain leads with a minted 32-hex share
    # label and a hashed user segment -- no internal id appears in it.
    domain_labels = str(body["workspace_domain"]).split(".")
    assert re.fullmatch(r"[a-f0-9]{32}", domain_labels[0])
    assert domain_labels[0] != _HOST_ID_STR
    assert domain_labels[1] == hashlib.sha256(_USER_STUB_USER_ID.encode()).hexdigest()[:32]
    assert body["workspace_domain"].endswith(f".us1.{_CONTENT_DOMAIN}")
    assert body["workspace_id"] == "agent-abc123"
    expected_domain = str(body["workspace_domain"])

    # The share materials were written into the container over the (faked) SSH.
    assert len(backend.written_container_files) == 1
    _host, port, files = backend.written_container_files[0]
    assert port == 2222
    assert set(files) == {
        "/home/user/workspace/data/.secrets/share.env",
        "/home/user/workspace/data/.secrets/share_grants.toml",
    }
    assert f"SHARE_WORKSPACE_DOMAIN={expected_domain}" in files["/home/user/workspace/data/.secrets/share.env"]
    assert _CHROME_ORIGIN in files["/home/user/workspace/data/.secrets/share.env"]
    assert _USER_STUB_EMAIL in files["/home/user/workspace/data/.secrets/share_grants.toml"]


def test_enable_sharing_never_reuses_a_stale_row_claimed_by_another_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )
    # A previous lease of this machine left a share row claimed by a different
    # workspace (the host is a mutable machine attribute).
    stale_domain = f"{'f' * 32}.{'0' * 32}.us1.{_CONTENT_DOMAIN}"
    backend.upsert_share(
        _HOST_ID_STR,
        _OWNER_LABEL,
        "us1",
        stale_domain,
        workspace_id="agent-" + "e" * 32,
        share_label="f" * 32,
    )

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 200
    body = resp.json()
    # The bring-up must not inherit the other workspace's identity or domain.
    assert body["workspace_id"] == "agent-abc123"
    assert body["workspace_domain"] != stale_domain


def test_enable_sharing_rejects_a_host_owned_by_another_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user="someone-else",
        host_id_str=_HOST_ID_STR,
    )

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 403
    assert backend.written_container_files == []


def test_enable_sharing_404s_for_an_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_share_env(monkeypatch)
    client, _backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 404


def test_enable_sharing_seeds_grants_if_absent_but_always_replaces_share_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-enabling sharing rotates the relay token, so share.env must be
    # replaced every time -- but the grants document belongs to the workspace
    # after the first seed, and an unconditional rewrite would silently revoke
    # every grant the user added since.
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )

    first = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())
    second = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(backend.written_container_files) == 2
    for seed_only_paths, (_host, _port, files) in zip(
        backend.written_container_seed_only_paths, backend.written_container_files, strict=True
    ):
        assert seed_only_paths == {"/home/user/workspace/data/.secrets/share_grants.toml"}
        assert "/home/user/workspace/data/.secrets/share.env" not in seed_only_paths
        assert set(files) == {
            "/home/user/workspace/data/.secrets/share.env",
            "/home/user/workspace/data/.secrets/share_grants.toml",
        }


def test_enable_sharing_reports_a_previously_recorded_entry_label(monkeypatch: pytest.MonkeyPatch) -> None:
    # The entry label is recorded by the frps NewProxy callback once the
    # workspace's tunnel claims its service labels; a re-enable must neither
    # wipe it (activation passes None; the COALESCE keeps the row's value)
    # nor stop reporting it.
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )

    first = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())
    assert first.status_code == 200
    assert first.json()["entry_label"] is None

    # Simulate the tunnel's NewProxy claim having recorded the shell label.
    share_row = backend.find_share(_HOST_ID_STR, _OWNER_LABEL)
    assert share_row is not None
    share_row["entry_label"] = "system_interface-elm7wydc"

    second = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert second.status_code == 200
    assert second.json()["entry_label"] == "system_interface-elm7wydc"
    share_row_after = backend.find_share(_HOST_ID_STR, _OWNER_LABEL)
    assert share_row_after is not None
    assert share_row_after["entry_label"] == "system_interface-elm7wydc"


def test_enable_sharing_conflicts_when_owned_host_is_not_leased(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host the caller owns but that is mid-release ('removing') is not leased,
    # so sharing cannot be enabled on it.
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_removing_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 409


def test_enable_sharing_fails_closed_when_container_key_is_not_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a pinned container host key there is no way to authenticate the
    # container, so the endpoint refuses to SSH (fail closed) rather than
    # trusting the host on first use.
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    row = backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )
    row.container_host_public_key = None

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 503
    assert backend.written_container_files == []


def test_enable_sharing_conflicts_when_the_desktop_app_rotated_the_container_host_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A desktop-adopted workspace serves a host key the row never learns (the
    # rotation is client-side by design); the mismatch is the signal that
    # sharing must be enabled from the desktop app.
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )
    backend.is_container_host_key_mismatched = True

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "workspace_managed_by_desktop"
    assert "desktop app" in detail["message"]
    assert backend.written_container_files == []


def test_enable_sharing_conflict_leaves_the_desktop_established_share_and_its_token_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The desktop app already shares this workspace (its own share row and
    # relay token). The server-side enable must refuse BEFORE rotating the
    # token: a rotation the container never receives would sever the tunnel
    # the desktop's share is running on.
    _install_share_env(monkeypatch)
    client, backend, _entitlements, _litellm = _make_pool_quota_test_client(monkeypatch)
    backend.add_leased_host(
        host_id=_HOST_DB_ID,
        version="v0.1.0",
        leased_to_user=_USER_STUB_USER_ID_PREFIX,
        host_id_str=_HOST_ID_STR,
    )
    user_label = derive_share_user_label(_USER_STUB_USER_ID)
    backend.add_share(_HOST_ID_STR, user_label, "us1", f"desktop-share.us1.{_CONTENT_DOMAIN}")
    desktop_token = {"token_hash": "desktop-token-hash", "host_id": _HOST_ID_STR, "user_id": user_label}
    backend.relay_token_rows.append(dict(desktop_token))
    backend.is_container_host_key_mismatched = True

    resp = client.post(f"/hosts/{_HOST_DB_ID}/enable-sharing", headers=_user_headers())

    assert resp.status_code == 409
    assert backend.relay_token_rows == [desktop_token]
    share = backend.find_share(_HOST_ID_STR, user_label)
    assert share is not None
    assert share["workspace_domain"] == f"desktop-share.us1.{_CONTENT_DOMAIN}"
    assert backend.written_container_files == []


# How long the loopback sshd's accept loop waits before re-checking for shutdown.
_SSHD_ACCEPT_POLL_SECONDS: Final[float] = 0.2


def _public_key_line(key: paramiko.PKey) -> str:
    return f"{key.get_name()} {key.get_base64()}"


@contextmanager
def _loopback_sshd(host_keys: Sequence[paramiko.PKey]) -> Iterator[int]:
    """Run an sshd on loopback that serves ``host_keys``; yield its port.

    Only the key exchange matters here (the probe never authenticates), so the
    server interface is paramiko's default, which admits nothing.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(_SSHD_ACCEPT_POLL_SECONDS)
    port = listener.getsockname()[1]

    served: list[paramiko.Transport] = []
    stop_event = threading.Event()

    def accept_loop() -> None:
        while not stop_event.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            transport = paramiko.Transport(connection)
            for host_key in host_keys:
                transport.add_server_key(host_key)
            # Handed an event so the negotiation runs on the transport's own
            # thread and a client that hangs up right after reading the host
            # key (the probe does) cannot fail this loop.
            transport.start_server(event=threading.Event(), server=paramiko.ServerInterface())
            served.append(transport)

    thread = threading.Thread(target=accept_loop, daemon=True, name="test-loopback-sshd")
    thread.start()
    try:
        yield port
    finally:
        stop_event.set()
        thread.join(timeout=5.0)
        for transport in served:
            transport.close()
        listener.close()


def test_container_host_key_probe_accepts_the_pinned_key() -> None:
    host_key = paramiko.ECDSAKey.generate()
    with _loopback_sshd([host_key]) as port:
        _assert_container_serves_pinned_host_key("127.0.0.1", port, _public_key_line(host_key))


def test_container_host_key_probe_rejects_a_key_other_than_the_pinned_one() -> None:
    served_key = paramiko.ECDSAKey.generate()
    pinned_key = paramiko.ECDSAKey.generate()
    with _loopback_sshd([served_key]) as port:
        with pytest.raises(paramiko.BadHostKeyException) as exc_info:
            _assert_container_serves_pinned_host_key("127.0.0.1", port, _public_key_line(pinned_key))
    assert exc_info.value.key.asbytes() == served_key.asbytes()
    assert exc_info.value.expected_key.asbytes() == pinned_key.asbytes()


def test_container_host_key_probe_negotiates_the_pinned_key_type() -> None:
    # An sshd serving several host keys presents the type the client prefers;
    # the probe must ask for the pinned one (as SSHClient.connect does for a
    # known host), not paramiko's default preference, or a healthy container
    # whose pin is a less-preferred type would read as rotated.
    preferred_key = paramiko.ECDSAKey.generate(bits=256)
    pinned_key = paramiko.ECDSAKey.generate(bits=384)
    with _loopback_sshd([preferred_key, pinned_key]) as port:
        _assert_container_serves_pinned_host_key("127.0.0.1", port, _public_key_line(pinned_key))
