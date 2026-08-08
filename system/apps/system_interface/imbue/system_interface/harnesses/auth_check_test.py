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
    return HarnessAuthCheck(command=command, display_name="Test")


def _pattern_check(output: str, pattern: str) -> HarnessAuthCheck:
    return HarnessAuthCheck(
        command=("sh", "-c", f"printf '%s' {output!r}"),
        display_name="Test",
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
        HarnessAuthCheck(command=("false",), display_name="Codex"),
    )
    reason = find_unauthenticated_harness_reason(HarnessType.CODEX)
    assert reason is not None
    assert "Codex" in reason


def test_signed_in_harness_yields_no_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        HARNESS_AUTH_CHECKS,
        HarnessType.CODEX,
        HarnessAuthCheck(command=("true",), display_name="Codex"),
    )
    assert find_unauthenticated_harness_reason(HarnessType.CODEX) is None
