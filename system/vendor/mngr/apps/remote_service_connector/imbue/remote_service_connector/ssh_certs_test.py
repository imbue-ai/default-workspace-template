import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.remote_service_connector.ssh_certs import BundleSource
from imbue.remote_service_connector.ssh_certs import ManagementSshCredentials
from imbue.remote_service_connector.ssh_certs import SSH_CERT_DICT_KEY_CONNECTOR
from imbue.remote_service_connector.ssh_certs import SSH_CERT_REREAD_MARGIN
from imbue.remote_service_connector.ssh_certs import SshCertificateBundle
from imbue.remote_service_connector.ssh_certs import SshCertificateBundleMissingError
from imbue.remote_service_connector.ssh_certs import gen2_management_credentials
from imbue.remote_service_connector.ssh_certs import generate_ed25519_private_key_pem_and_public_key
from imbue.remote_service_connector.ssh_certs import management_credentials_for_generation

_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _sign_with_throwaway_ca(tmp_path: Path, public_key: str) -> str:
    """A certificate for ``public_key`` from a throwaway CA, the way Vault's SSH engine would issue one."""
    ca_path = tmp_path / "ca"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca_path)], check=True)
    public_key_path = tmp_path / "id.pub"
    public_key_path.write_text(public_key + "\n")
    subprocess.run(
        ["ssh-keygen", "-q", "-s", str(ca_path), "-I", "connector:test", "-n", "mngr-vm", str(public_key_path)],
        check=True,
    )
    return (tmp_path / "id-cert.pub").read_text().strip()


def test_generated_keypair_is_a_valid_openssh_ed25519_key(tmp_path: Path) -> None:
    private_key_pem, public_key = generate_ed25519_private_key_pem_and_public_key()
    key_path = tmp_path / "id"
    key_path.write_text(private_key_pem.get_secret_value())
    key_path.chmod(0o600)
    derived = subprocess.run(["ssh-keygen", "-y", "-f", str(key_path)], check=True, capture_output=True, text=True)
    assert derived.stdout.split()[:2] == public_key.split()[:2]


def test_credentials_present_the_certificate_when_one_is_attached(tmp_path: Path) -> None:
    private_key_pem, public_key = generate_ed25519_private_key_pem_and_public_key()
    certificate = _sign_with_throwaway_ca(tmp_path, public_key)
    certified = ManagementSshCredentials(private_key_pem=private_key_pem, certificate=certificate).to_paramiko_key()
    assert certified.public_blob is not None
    assert certified.public_blob.key_type == "ssh-ed25519-cert-v01@openssh.com"
    raw = ManagementSshCredentials(private_key_pem=private_key_pem, certificate=None).to_paramiko_key()
    assert raw.public_blob is None


def test_bundle_round_trips_through_the_dict_entry_shape() -> None:
    bundle = SshCertificateBundle(
        private_key_pem=SecretStr("KEY-MATERIAL"),
        certificate="cert",
        expires_at=_NOW + timedelta(hours=8),
        role="connector",
    )
    restored = SshCertificateBundle.from_dict_entry(bundle.to_dict_entry())
    assert restored == bundle
    # The entry is plain strings (a Modal Dict value) and never leaks the key in its repr.
    assert set(bundle.to_dict_entry()) == {"private_key_pem", "certificate", "expires_at", "role"}
    assert "KEY-MATERIAL" not in repr(bundle)


def test_bundle_freshness_uses_the_reread_margin() -> None:
    bundle = SshCertificateBundle(private_key_pem=SecretStr("pem"), certificate="c", expires_at=_NOW, role="connector")
    assert bundle.is_fresh_at(_NOW - SSH_CERT_REREAD_MARGIN)
    assert not bundle.is_fresh_at(_NOW - SSH_CERT_REREAD_MARGIN + timedelta(seconds=1))


def _source_over(entries: dict[str, dict[str, str] | None]) -> tuple[BundleSource, list[str]]:
    reads: list[str] = []

    def reader(dict_key: str) -> dict[str, str] | None:
        reads.append(dict_key)
        return entries.get(dict_key)

    return BundleSource(reader=reader), reads


def test_gen2_credentials_are_read_once_and_cached_until_the_margin() -> None:
    fresh = SshCertificateBundle(
        private_key_pem=SecretStr("pem"),
        certificate="cert",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        role="connector",
    )
    source, reads = _source_over({SSH_CERT_DICT_KEY_CONNECTOR: fresh.to_dict_entry()})
    first = gen2_management_credentials(source)
    second = management_credentials_for_generation(2, source)
    assert first == second
    assert first.certificate == "cert"
    assert reads == [SSH_CERT_DICT_KEY_CONNECTOR]


def test_gen2_credentials_refuse_without_a_bundle_or_with_an_expired_one() -> None:
    empty_source, _reads = _source_over({})
    with pytest.raises(SshCertificateBundleMissingError, match="has not stored one"):
        gen2_management_credentials(empty_source)
    expired = SshCertificateBundle(
        private_key_pem=SecretStr("pem"),
        certificate="cert",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        role="connector",
    )
    expired_source, _reads = _source_over({SSH_CERT_DICT_KEY_CONNECTOR: expired.to_dict_entry()})
    with pytest.raises(SshCertificateBundleMissingError, match="expired"):
        gen2_management_credentials(expired_source)
    with pytest.raises(SshCertificateBundleMissingError, match="no management certificate source"):
        gen2_management_credentials(BundleSource())


def test_gen1_credentials_are_the_static_pool_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POOL_SSH_PRIVATE_KEY", "pool-pem")
    credentials = management_credentials_for_generation(1)
    assert credentials.private_key_pem.get_secret_value() == "pool-pem"
    assert credentials.certificate is None
