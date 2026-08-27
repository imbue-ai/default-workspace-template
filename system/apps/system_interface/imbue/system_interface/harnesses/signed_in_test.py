"""Tests for the promote gate.

This module decides whether a folder a sign-in just wrote to actually holds a working
credential, and it is the only thing standing between "the file was written" and "the
account is offered to the user". Every arm below is a judgement about a CLI's output, so
the runner is injected rather than shelling out to whatever binaries this machine has.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.signed_in import SignedIn
from imbue.system_interface.harnesses.signed_in import is_signed_in
from imbue.concurrency_group.errors import ProcessError


class _Finished:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "", is_timed_out: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.is_timed_out = is_timed_out


def _runner_returning(finished: _Finished, seen: dict[str, Any] | None = None):
    def run(**kwargs: Any) -> _Finished:
        if seen is not None:
            seen.update(kwargs)
        return finished

    return run


# ----- pi: no exit code to read, so the string is the whole test ---------------------------


def test_pi_reads_its_actual_signed_out_message(tmp_path: Path) -> None:
    """Measured against 0.83.0 and 0.84.1: an empty dir, an unknown provider id and a
    malformed auth.json all print this and exit ZERO. Matching anything else makes NO
    unreachable and the gate a round trip that always says yes."""
    runner = _runner_returning(
        _Finished(returncode=0, stdout="No models available. Use /login to log into a provider")
    )
    assert is_signed_in(HarnessType.PI_CODING, tmp_path, runner) is SignedIn.NO


def test_pi_naming_models_is_a_working_credential(tmp_path: Path) -> None:
    runner = _runner_returning(_Finished(returncode=0, stdout="groq/llama-3.3-70b\ngroq/qwen"))
    assert is_signed_in(HarnessType.PI_CODING, tmp_path, runner) is SignedIn.YES


# ----- claude: the server's own credentials must not answer for the folder -----------------


def test_the_probe_does_not_carry_the_servers_own_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude auth status --json` reports loggedIn on an ambient ANTHROPIC_API_KEY alone, so
    an upgraded workspace's leftover key would commit an EMPTY account folder as signed in,
    make it the most-recently-used, and launch every later chat with no credential."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-leftover")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen: dict[str, Any] = {}
    runner = _runner_returning(_Finished(returncode=0), seen)

    is_signed_in(HarnessType.CLAUDE, tmp_path, runner)

    env = seen["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin", "the ambient env is layered under, not replaced"
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)


def test_claude_and_codex_are_decided_by_the_exit_code(tmp_path: Path) -> None:
    for harness in (HarnessType.CLAUDE, HarnessType.CODEX):
        assert is_signed_in(harness, tmp_path, _runner_returning(_Finished(returncode=0))) is SignedIn.YES
        assert is_signed_in(harness, tmp_path, _runner_returning(_Finished(returncode=1))) is SignedIn.NO


# ----- the three-way answer ----------------------------------------------------------------


def test_a_probe_that_cannot_run_is_unknown_not_signed_out(tmp_path: Path) -> None:
    """Collapsing this into NO would delete a folder the user just completed a browser OAuth
    into, because the probes shell out to CLIs that fetch over the network."""

    def run(**_kwargs: Any) -> _Finished:
        raise ProcessError("no such binary")

    assert is_signed_in(HarnessType.CLAUDE, tmp_path, run) is SignedIn.UNKNOWN


def test_a_timeout_is_unknown(tmp_path: Path) -> None:
    runner = _runner_returning(_Finished(is_timed_out=True))
    assert is_signed_in(HarnessType.ANTIGRAVITY, tmp_path, runner) is SignedIn.UNKNOWN


def test_agy_failing_for_any_other_reason_is_unknown(tmp_path: Path) -> None:
    """agy fetches the catalogue over the network, so a non-zero exit means "signed out OR
    the network blinked". Its text distinguishes them; without the guard a network failure
    would fall through to YES and commit a signed-out account."""
    runner = _runner_returning(_Finished(returncode=1, stderr="Error: connection reset"))
    assert is_signed_in(HarnessType.ANTIGRAVITY, tmp_path, runner) is SignedIn.UNKNOWN


def test_agy_saying_sign_in_is_signed_out(tmp_path: Path) -> None:
    runner = _runner_returning(_Finished(returncode=1, stderr="Please sign in to view available models."))
    assert is_signed_in(HarnessType.ANTIGRAVITY, tmp_path, runner) is SignedIn.NO


def test_a_harness_with_no_probe_is_taken_at_its_word(tmp_path: Path) -> None:
    def run(**_kwargs: Any) -> _Finished:
        raise AssertionError("nothing to ask")

    assert is_signed_in(HarnessType.OPENCODE, tmp_path, run) is SignedIn.YES
