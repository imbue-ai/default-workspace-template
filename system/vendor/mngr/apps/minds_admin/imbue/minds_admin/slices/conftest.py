import os
import subprocess
from pathlib import Path

import pytest

from imbue.minds_admin.slices.cutover_state import CutoverStateStore
from imbue.minds_admin.slices.operator_identity import OPERATOR_IDENTITY_DIR_ENV_VAR
from imbue.minds_admin.slices.runtime_benchmark import _CONTAINER_PRELUDE
from imbue.minds_admin.slices.testing import FakeVaultSigner
from imbue.minds_admin.slices.testing import make_fake_vault_signer


@pytest.fixture
def local_container_prelude(tmp_path: Path) -> str:
    """The benchmark's container prelude, runnable by the local bash with its workspace dir pointed at a scratch tree.

    The prelude times with bash 5's EPOCHREALTIME (it only ever runs inside the
    Linux workspace container); a local bash without it (macOS's /bin/bash 3.2)
    cannot execute it, so tests using this fixture are skipped there.
    """
    if subprocess.run(["bash", "-c", '[ -n "${EPOCHREALTIME:-}" ]'], capture_output=True).returncode != 0:
        pytest.skip("the local bash has no EPOCHREALTIME (bash < 5)")
    return _CONTAINER_PRELUDE.replace("cd /home/user/workspace", f"cd {tmp_path}")


@pytest.fixture
def cutover_state_store(tmp_path: Path) -> CutoverStateStore:
    """A cutover state dir under the test's tmp_path with its layout created."""
    store = CutoverStateStore(root=tmp_path / "cutover")
    store.ensure_layout()
    return store


@pytest.fixture
def fake_vault_signer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeVaultSigner:
    """A fake ``vault`` first on PATH, with the operator identity root pointed at a scratch dir.

    Signing the operator's management SSH certificate then runs entirely
    against the fake (no real Vault login); ``sign_count`` tells a test how
    many times it was asked to sign.
    """
    monkeypatch.setenv(OPERATOR_IDENTITY_DIR_ENV_VAR, str(tmp_path / "identity"))
    signer = make_fake_vault_signer(tmp_path / "fake-vault")
    monkeypatch.setenv("PATH", f"{signer.binary_dir}:{os.environ['PATH']}")
    return signer
