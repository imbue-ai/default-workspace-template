import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds_admin.slices.operator_identity import OPERATOR_IDENTITY_DIR_ENV_VAR
from imbue.minds_admin.slices.operator_identity import OperatorCertificateMeta
from imbue.minds_admin.slices.operator_identity import _read_certificate_meta_or_none
from imbue.minds_admin.slices.operator_identity import ensure_operator_ssh_identity
from imbue.minds_admin.slices.operator_identity import is_certificate_fresh_enough
from imbue.minds_admin.slices.operator_identity import management_identities
from imbue.minds_admin.slices.operator_identity import operator_identity_dir
from imbue.minds_admin.slices.operator_identity import operator_identity_root
from imbue.minds_admin.slices.operator_identity import operator_ssh_key_path
from imbue.minds_admin.slices.testing import FakeVaultSigner
from imbue.mngr.providers.ssh_utils import ssh_certificate_path_for
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import FIRST_QEMU_BOX_GENERATION

GEN1_BOX_GENERATION = FIRST_QEMU_BOX_GENERATION - 1


def _pool_key_pem() -> str:
    return "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-pool-key\n-----END OPENSSH PRIVATE KEY-----"


def test_operator_identity_root_defaults_and_honors_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(OPERATOR_IDENTITY_DIR_ENV_VAR, raising=False)
    assert operator_identity_root() == Path("~/.mindsadmin").expanduser()
    monkeypatch.setenv(OPERATOR_IDENTITY_DIR_ENV_VAR, str(tmp_path))
    assert operator_identity_root() == tmp_path


def test_operator_identity_dir_creates_a_private_per_tier_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(OPERATOR_IDENTITY_DIR_ENV_VAR, str(tmp_path))
    identity_dir = operator_identity_dir("dev")
    assert identity_dir == tmp_path / "dev"
    assert stat.S_IMODE(identity_dir.stat().st_mode) == 0o700


def test_is_certificate_fresh_enough_at_the_minimum_remaining_threshold() -> None:
    now = datetime.now(timezone.utc)
    fresh = OperatorCertificateMeta(signed_at=now, expires_at=now + timedelta(hours=6), tier="dev")
    stale = OperatorCertificateMeta(
        signed_at=now, expires_at=now + timedelta(hours=6) - timedelta(seconds=1), tier="dev"
    )
    assert is_certificate_fresh_enough(fresh, now) is True
    assert is_certificate_fresh_enough(stale, now) is False


def test_read_certificate_meta_or_none_returns_none_when_no_sidecar_exists(tmp_path: Path) -> None:
    assert _read_certificate_meta_or_none(tmp_path / "ssh_id") is None


def test_read_certificate_meta_or_none_returns_none_when_the_certificate_file_is_missing(tmp_path: Path) -> None:
    private_key_path = tmp_path / "ssh_id"
    meta_path = tmp_path / "ssh_id-cert.json"
    meta_path.write_text(
        json.dumps({"signed_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-01T12:00:00Z", "tier": "dev"})
    )
    # No "ssh_id-cert.pub" written, so the sidecar is ignored.
    assert _read_certificate_meta_or_none(private_key_path) is None


def test_read_certificate_meta_or_none_ignores_a_corrupt_sidecar(tmp_path: Path) -> None:
    private_key_path = tmp_path / "ssh_id"
    ssh_certificate_path_for(private_key_path).write_text("ssh-ed25519-cert-v01@openssh.com AAAA\n")
    (tmp_path / "ssh_id-cert.json").write_text("not json")
    assert _read_certificate_meta_or_none(private_key_path) is None


def test_read_certificate_meta_or_none_parses_a_valid_sidecar(tmp_path: Path) -> None:
    private_key_path = tmp_path / "ssh_id"
    ssh_certificate_path_for(private_key_path).write_text("ssh-ed25519-cert-v01@openssh.com AAAA\n")
    (tmp_path / "ssh_id-cert.json").write_text(
        json.dumps({"signed_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-01T12:00:00Z", "tier": "dev"})
    )
    meta = _read_certificate_meta_or_none(private_key_path)
    assert meta is not None
    assert meta.tier == "dev"


def test_private_key_path_for_gen1_creates_and_reuses_a_temp_dir_removed_on_close() -> None:
    with management_identities(tier=None, resolve_gen1_pool_private_key_pem=_pool_key_pem) as identities:
        first = identities.private_key_path_for(GEN1_BOX_GENERATION)
        second = identities.private_key_path_for(GEN1_BOX_GENERATION)
        assert first == second
        assert "fake-pool-key" in first.read_text()
        key_dir = first.parent
    assert not key_dir.exists()


def test_private_key_path_for_gen2_returns_the_preset_operator_key_without_resolving_gen1(tmp_path: Path) -> None:
    preset_path = tmp_path / "ssh_id"
    preset_path.write_text("preset\n")

    def _fail() -> str:
        raise AssertionError("the gen-1 pool key should never be resolved for a gen-2 box")

    with management_identities(tier="dev", resolve_gen1_pool_private_key_pem=_fail) as identities:
        identities.operator_key_path = preset_path
        assert identities.private_key_path_for(FIRST_QEMU_BOX_GENERATION) == preset_path


def test_private_key_path_for_gen2_without_an_activated_tier_raises() -> None:
    with management_identities(tier=None, resolve_gen1_pool_private_key_pem=_pool_key_pem) as identities:
        with pytest.raises(BareMetalProvisioningError, match="activated minds env"):
            identities.private_key_path_for(FIRST_QEMU_BOX_GENERATION)


def test_concurrent_gen1_resolution_creates_exactly_one_temp_dir_and_leaks_none() -> None:
    """Regression test: concurrent workers used to race on the None-check and leak temp dirs."""
    with management_identities(tier=None, resolve_gen1_pool_private_key_pem=_pool_key_pem) as identities:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: identities.private_key_path_for(GEN1_BOX_GENERATION), range(16)))
        resolved_dirs = sorted({path.parent for path in results})
        assert len(resolved_dirs) == 1, f"expected every thread to observe the same temp dir, got {resolved_dirs}"
        assert identities.gen1_key_dir == resolved_dirs[0]
    for stale_dir in resolved_dirs:
        assert not stale_dir.exists()


def test_concurrent_gen2_resolution_signs_exactly_one_certificate(fake_vault_signer: FakeVaultSigner) -> None:
    """Regression test: concurrent workers used to race on the None-check and could sign a certificate more than once."""
    with management_identities(tier="dev", resolve_gen1_pool_private_key_pem=_pool_key_pem) as identities:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: identities.private_key_path_for(FIRST_QEMU_BOX_GENERATION), range(8)))
    assert len({str(path) for path in results}) == 1
    assert fake_vault_signer.sign_count == 1


def test_ensure_operator_ssh_identity_reuses_a_fresh_certificate_without_resigning(
    fake_vault_signer: FakeVaultSigner,
) -> None:
    first_path = ensure_operator_ssh_identity("dev")
    second_path = ensure_operator_ssh_identity("dev")
    assert first_path == second_path
    assert fake_vault_signer.sign_count == 1
    assert ssh_certificate_path_for(operator_ssh_key_path("dev")).is_file()


def test_ensure_operator_ssh_identity_surfaces_a_refused_sign_as_a_provisioning_error(
    fake_vault_signer: FakeVaultSigner,
) -> None:
    fake_vault_signer.install(is_sign_refused=True)
    with pytest.raises(BareMetalProvisioningError, match="vault login"):
        ensure_operator_ssh_identity("dev")


def test_concurrent_ensure_operator_ssh_identity_calls_sign_exactly_once(fake_vault_signer: FakeVaultSigner) -> None:
    """Regression test: two concurrent CLI invocations used to be able to both decide no fresh certificate exists
    and race to sign and write one; the on-disk lock now serializes them onto a single sign."""
    # A short pause between the check and the increment widens the race window
    # a real concurrent Vault round trip would have.
    fake_vault_signer.install(sleep_seconds=0.05)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: ensure_operator_ssh_identity("dev"), range(6)))
    assert len({str(path) for path in results}) == 1
    assert fake_vault_signer.sign_count == 1
