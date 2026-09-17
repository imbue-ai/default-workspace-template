"""The account-models probe's failure reporting, driven against a stub ``codex`` on PATH.

The success path needs a real ``codex app-server`` (a bound unix socket speaking the app-server
protocol) and lives in the live exercise of the switch dialog, not here. What is worth pinning in a
unit test is the arm a real workspace actually hits: a codex that IS installed and does not come up.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from imbue.chat.harnesses.codex.account_models import AccountModelProbeError
from imbue.chat.harnesses.codex.account_models import probe_codex_account_models


def _stub_codex_on_path(bin_dir: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Put an executable ``codex`` running ``script`` at the front of PATH, alone."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "codex"
    stub.write_text(script)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def test_a_codex_that_exits_without_binding_is_reported_at_once_in_its_own_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed-out or broken codex exits immediately; the probe must say so rather than wait out its
    bind ceiling and then blame the socket. The switch dialog's request is held open for as long as this
    takes, and the reason codex printed is the only one that tells the user what to do."""
    _stub_codex_on_path(
        tmp_path / "bin", monkeypatch, "#!/bin/sh\necho \"not signed in; run 'codex login'\" >&2\nexit 1\n"
    )
    started = time.monotonic()
    with pytest.raises(AccountModelProbeError) as caught:
        probe_codex_account_models(tmp_path)
    elapsed = time.monotonic() - started
    assert "not signed in" in str(caught.value)
    # Generously under the 15s ceiling a socket-only wait would have run to.
    assert elapsed < 5.0, f"the probe took {elapsed:.1f}s to notice a codex that had already exited"
