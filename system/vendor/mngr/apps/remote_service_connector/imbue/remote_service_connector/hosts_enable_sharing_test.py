"""Tests for the server-driven enable-sharing endpoint and its composition helpers."""

from uuid import UUID

import pytest

from imbue.remote_service_connector.hosts import build_owner_grants_toml
from imbue.remote_service_connector.hosts import build_share_env_text
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
    expected_domain = f"{_HOST_ID_STR}.{_OWNER_LABEL}.us1.{_CONTENT_DOMAIN}"
    assert body["workspace_domain"] == expected_domain

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
