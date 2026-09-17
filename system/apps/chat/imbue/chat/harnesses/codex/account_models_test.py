"""The account-models probe's failure reporting, driven against a stub ``codex`` on PATH.

The success path needs a real ``codex app-server`` (a bound unix socket speaking the app-server
protocol) and lives in the live exercise of the switch dialog, not here. What is worth pinning in a
unit test is the arm a real workspace actually hits: a codex that IS installed and does not come up.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from imbue.chat.harnesses.codex.account_models import AccountModelProbeError
from imbue.chat.harnesses.codex.account_models import probe_codex_account_models


def _stub_codex_on_path(bin_dir: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Put an executable ``codex`` running ``script`` at the FRONT of PATH, ahead of any real one.

    Prepended rather than substituted for the whole of PATH: the probe hands the child the server's
    environment, and a shell script with nothing but this directory on PATH cannot reach the ordinary
    utilities a stub wants to use.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "codex"
    stub.write_text(script)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


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


def test_two_probes_of_one_account_do_not_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both probes of an account want the same socket path, so an overlap has each unlinking the other's
    live socket. The stub records when it is running; the two runs must not interleave."""
    marker = tmp_path / "runs.log"
    _stub_codex_on_path(
        tmp_path / "bin",
        monkeypatch,
        f'#!/bin/sh\necho entered >> "{marker}"\nsleep 0.3\necho left >> "{marker}"\nexit 1\n',
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(probe_codex_account_models, tmp_path) for _ in range(2)]:
            with pytest.raises(AccountModelProbeError):
                future.result()
    assert marker.read_text().split() == ["entered", "left", "entered", "left"]
