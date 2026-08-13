"""Unit tests for the alt-harness sign-in preflight.

The status commands are exercised with portable trivial commands (``true``/``false``/``sh
-c``) that stand in for a real harness CLI, so the two detection modes (exit code and output
pattern) and the fail-closed behavior are all checked without any
harness installed.
"""

import pytest

from imbue.system_interface.harnesses.auth_check import HARNESS_AUTH_CHECKS
from imbue.system_interface.harnesses.auth_check import HarnessAuthCheck
from imbue.system_interface.harnesses.auth_check import _is_signed_in
from imbue.system_interface.harnesses.auth_check import find_unauthenticated_harness_reason
from imbue.system_interface.harnesses.harness_type import HarnessType


def _exit_code_check(command: tuple[str, ...]) -> HarnessAuthCheck:
    return HarnessAuthCheck(command=command, display_name="Test", signin_instructions="Sign in.")


def _pattern_check(output: str, pattern: str) -> HarnessAuthCheck:
    return HarnessAuthCheck(
        command=("sh", "-c", f"printf '%s' {output!r}"),
        display_name="Test",
        signin_instructions="Sign in.",
        unauthenticated_output_pattern=pattern,
    )


def test_signed_in_when_status_command_exits_zero() -> None:
    assert _is_signed_in(_exit_code_check(("true",))) is True


def test_signed_out_when_status_command_exits_nonzero() -> None:
    assert _is_signed_in(_exit_code_check(("false",))) is False


def test_signed_out_when_binary_is_missing() -> None:
    """Fail-closed: a command that cannot start counts as signed out."""
    assert _is_signed_in(_exit_code_check(("this-harness-binary-does-not-exist",))) is False


def test_pattern_mode_signed_out_when_pattern_present() -> None:
    check = _pattern_check("No models available", r"No models available")
    assert _is_signed_in(check) is False


def test_pattern_mode_signed_in_when_pattern_absent() -> None:
    check = _pattern_check("2 models available", r"No models available")
    assert _is_signed_in(check) is True


def test_claude_is_never_gated() -> None:
    assert find_unauthenticated_harness_reason(HarnessType.CLAUDE) is None


def test_signed_out_harness_yields_a_reason_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        HARNESS_AUTH_CHECKS,
        HarnessType.CODEX,
        HarnessAuthCheck(command=("false",), display_name="Codex", signin_instructions="Sign in."),
    )
    reason = find_unauthenticated_harness_reason(HarnessType.CODEX)
    assert reason is not None
    assert "Codex" in reason


def test_signed_in_harness_yields_no_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        HARNESS_AUTH_CHECKS,
        HarnessType.CODEX,
        HarnessAuthCheck(command=("true",), display_name="Codex", signin_instructions="Sign in."),
    )
    assert find_unauthenticated_harness_reason(HarnessType.CODEX) is None


def test_codex_refusal_gives_the_verbatim_signin_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed-out Codex refusal names the harness and tells the user exactly how to sign in."""
    real = HARNESS_AUTH_CHECKS[HarnessType.CODEX]
    monkeypatch.setitem(
        HARNESS_AUTH_CHECKS,
        HarnessType.CODEX,
        HarnessAuthCheck(command=("false",), display_name=real.display_name, signin_instructions=real.signin_instructions),
    )
    reason = find_unauthenticated_harness_reason(HarnessType.CODEX)
    assert reason is not None
    assert "Codex is not signed in on this workspace." in reason
    assert "Go to New tab (+) → New terminal → run `codex`" in reason


def test_pi_refusal_gives_the_verbatim_signin_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signed-out Pi refusal names the harness and gives its distinct /login instruction."""
    real = HARNESS_AUTH_CHECKS[HarnessType.PI_CODING]
    monkeypatch.setitem(
        HARNESS_AUTH_CHECKS,
        HarnessType.PI_CODING,
        HarnessAuthCheck(command=("false",), display_name=real.display_name, signin_instructions=real.signin_instructions),
    )
    reason = find_unauthenticated_harness_reason(HarnessType.PI_CODING)
    assert reason is not None
    assert "Pi is not signed in on this workspace." in reason
    assert "Go to New tab (+) → New terminal → run `pi` → type `/login`" in reason
