import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import httpx
import pytest
from pydantic import SecretStr

from imbue.remote_service_connector.ssh_certs import SSH_CERT_DICT_KEY_ANALYTICS
from imbue.remote_service_connector.ssh_certs import SSH_CERT_DICT_KEY_CONNECTOR
from imbue.remote_service_connector.ssh_certs import SSH_CERT_TTL
from imbue.remote_service_connector.vault_ssh_signer import VaultSshSignError
from imbue.remote_service_connector.vault_ssh_signer import VaultSshSignerConfig
from imbue.remote_service_connector.vault_ssh_signer import VaultSshSignerNotConfiguredError
from imbue.remote_service_connector.vault_ssh_signer import certificate_refresh_summary
from imbue.remote_service_connector.vault_ssh_signer import is_refresh_overdue
from imbue.remote_service_connector.vault_ssh_signer import load_vault_ssh_signer_config
from imbue.remote_service_connector.vault_ssh_signer import sign_management_bundles

_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _config() -> VaultSshSignerConfig:
    return VaultSshSignerConfig(
        vault_addr="https://vault.test",
        vault_namespace="admin",
        mount="minds-dev-ssh",
        approle_role_id="role-id",
        approle_secret_id=SecretStr("secret-id"),
    )


class _FakeVault:
    """An httpx transport handler standing in for Vault's AppRole login and SSH sign endpoints."""

    def __init__(self, *, is_sign_refused: bool = False) -> None:
        self.requests: list[tuple[str, dict, dict]] = []
        self.is_sign_refused = is_sign_refused

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request.url.path, body, dict(request.headers)))
        if request.url.path == "/v1/auth/approle/login":
            assert body == {"role_id": "role-id", "secret_id": "secret-id"}
            return httpx.Response(200, json={"auth": {"client_token": "hvs.token"}})
        if request.url.path.startswith("/v1/minds-dev-ssh/sign/"):
            if self.is_sign_refused:
                return httpx.Response(403, json={"errors": ["permission denied"]})
            role = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "data": {"signed_key": f"ssh-ed25519-cert-v01@openssh.com AAAA{role} {body['public_key'][:20]}\n"}
                },
            )
        return httpx.Response(404)


def test_sign_management_bundles_logs_in_once_and_signs_each_role_with_its_principals() -> None:
    fake_vault = _FakeVault()
    with httpx.Client(transport=httpx.MockTransport(fake_vault)) as client:
        bundles = sign_management_bundles(_config(), now=_NOW, http_client=client)
    assert set(bundles) == {SSH_CERT_DICT_KEY_CONNECTOR, SSH_CERT_DICT_KEY_ANALYTICS}
    connector = bundles[SSH_CERT_DICT_KEY_CONNECTOR]
    assert connector.certificate.startswith("ssh-ed25519-cert-v01@openssh.com AAAAconnector")
    assert connector.expires_at == _NOW + SSH_CERT_TTL
    assert connector.role == "connector"
    # A fresh key per bundle: the two roles never share a private key.
    assert connector.private_key_pem != bundles[SSH_CERT_DICT_KEY_ANALYTICS].private_key_pem
    paths = [path for path, _body, _headers in fake_vault.requests]
    assert paths == ["/v1/auth/approle/login", "/v1/minds-dev-ssh/sign/connector", "/v1/minds-dev-ssh/sign/analytics"]
    sign_bodies = {path.rsplit("/", 1)[1]: body for path, body, _headers in fake_vault.requests[1:]}
    assert sign_bodies["connector"]["valid_principals"] == "mngr-service,mngr-vm,mngr-container"
    assert sign_bodies["analytics"]["valid_principals"] == "mngr-vm,mngr-container"
    assert sign_bodies["connector"]["ttl"] == f"{int(SSH_CERT_TTL.total_seconds())}s"
    assert sign_bodies["connector"]["cert_type"] == "user"
    # The login token rides only on the sign calls, the namespace on every call.
    login_headers = fake_vault.requests[0][2]
    sign_headers = fake_vault.requests[1][2]
    assert "x-vault-token" not in login_headers
    assert sign_headers["x-vault-token"] == "hvs.token"
    assert sign_headers["x-vault-namespace"] == "admin"


def test_sign_management_bundles_surfaces_a_refused_sign() -> None:
    with httpx.Client(transport=httpx.MockTransport(_FakeVault(is_sign_refused=True))) as client:
        with pytest.raises(VaultSshSignError, match="403"):
            sign_management_bundles(_config(), now=_NOW, http_client=client)


def test_load_vault_ssh_signer_config_requires_the_approle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_SSH_APPROLE_ROLE_ID", raising=False)
    monkeypatch.delenv("VAULT_SSH_APPROLE_SECRET_ID", raising=False)
    with pytest.raises(VaultSshSignerNotConfiguredError):
        load_vault_ssh_signer_config("dev")
    monkeypatch.setenv("VAULT_SSH_APPROLE_ROLE_ID", "r")
    monkeypatch.setenv("VAULT_SSH_APPROLE_SECRET_ID", "s")
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    config = load_vault_ssh_signer_config("staging")
    assert config.mount == "minds-staging-ssh"
    assert config.vault_namespace == "admin"
    assert config.vault_addr.startswith("https://")


def test_refresh_summary_and_overdue_check() -> None:
    fake_vault = _FakeVault()
    with httpx.Client(transport=httpx.MockTransport(fake_vault)) as client:
        bundles = sign_management_bundles(_config(), now=_NOW, http_client=client)
    assert certificate_refresh_summary(bundles) == {
        "analytics": (_NOW + SSH_CERT_TTL).isoformat(),
        "connector": (_NOW + SSH_CERT_TTL).isoformat(),
    }
    assert not is_refresh_overdue(_NOW + SSH_CERT_TTL, _NOW + timedelta(hours=2))
    assert is_refresh_overdue(_NOW + SSH_CERT_TTL, _NOW + timedelta(hours=5))
