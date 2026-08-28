"""Tests for the alt-harness auth preflight.

The checks run real subprocesses deliberately (``true`` / ``false`` / ``echo``): the unit
under test IS the run-a-CLI-and-read-its-verdict path, so a stub subprocess would test
nothing. The commands are constructed checks, not the real CLIs; the two verbatim-refusal
tests read the real check's wording off the harness spec.
"""

from imbue.system_interface.harnesses.auth_check import ANTIGRAVITY_AUTH_CHECK
from imbue.system_interface.harnesses.auth_check import CODEX_AUTH_CHECK
from imbue.system_interface.harnesses.auth_check import HarnessAuthCheck
from imbue.system_interface.harnesses.auth_check import PI_AUTH_CHECK
from imbue.system_interface.harnesses.auth_check import find_unauthenticated_harness_reason
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.registry import get_harness_spec


def test_harness_without_a_check_is_cleared_to_launch() -> None:
    """claude registers no auth gate (its auth lives in the shared ~/.claude)."""
    assert get_harness_spec(HarnessType.CLAUDE).auth_check is None
    assert find_unauthenticated_harness_reason(None) is None


def test_specs_carry_the_registered_checks() -> None:
    """The registry is the ONE per-harness table; the checks live on the specs."""
    assert get_harness_spec(HarnessType.CODEX).auth_check is CODEX_AUTH_CHECK
    assert get_harness_spec(HarnessType.PI_CODING).auth_check is PI_AUTH_CHECK
    assert get_harness_spec(HarnessType.ANTIGRAVITY).auth_check is ANTIGRAVITY_AUTH_CHECK


def test_signed_out_harness_yields_a_reason_naming_it() -> None:
    check = HarnessAuthCheck(command=("false",), display_name="Codex", signin_instructions="Sign in.")
    reason = find_unauthenticated_harness_reason(check)
    assert reason is not None
    assert "Codex" in reason


def test_signed_in_harness_yields_no_reason() -> None:
    check = HarnessAuthCheck(command=("true",), display_name="Codex", signin_instructions="Sign in.")
    assert find_unauthenticated_harness_reason(check) is None


def test_codex_refusal_gives_the_verbatim_signin_instructions() -> None:
    """A signed-out Codex refusal names the harness and tells the user exactly how to sign in."""
    check = HarnessAuthCheck(
        command=("false",),
        display_name=CODEX_AUTH_CHECK.display_name,
        signin_instructions=CODEX_AUTH_CHECK.signin_instructions,
    )
    reason = find_unauthenticated_harness_reason(check)
    assert reason is not None
    assert "Codex is not signed in on this workspace." in reason
    assert "Go to New tab (+) → New terminal → run `codex`" in reason


def test_pi_refusal_gives_the_verbatim_signin_instructions() -> None:
    """A signed-out Pi refusal names the harness and gives its distinct /login instruction."""
    check = HarnessAuthCheck(
        command=("false",),
        display_name=PI_AUTH_CHECK.display_name,
        signin_instructions=PI_AUTH_CHECK.signin_instructions,
    )
    reason = find_unauthenticated_harness_reason(check)
    assert reason is not None
    assert "Pi is not signed in on this workspace." in reason
    assert "Go to New tab (+) → New terminal → run `pi` → type `/login`" in reason


def test_output_pattern_check_reads_the_signal_from_stdout() -> None:
    """pi's CLI exits 0 signed out; the signal is the ``No models available`` line."""
    signed_out = HarnessAuthCheck(
        command=("echo", "No models available"),
        display_name="Pi",
        signin_instructions="Sign in.",
        unauthenticated_output_pattern=r"No models available",
    )
    assert find_unauthenticated_harness_reason(signed_out) is not None
    signed_in = HarnessAuthCheck(
        command=("echo", "gpt-things"),
        display_name="Pi",
        signin_instructions="Sign in.",
        unauthenticated_output_pattern=r"No models available",
    )
    assert find_unauthenticated_harness_reason(signed_in) is None


def test_missing_binary_fails_closed() -> None:
    """A CLI we cannot run at all must not clear the harness to launch."""
    check = HarnessAuthCheck(
        command=("/nonexistent/definitely-not-a-binary",),
        display_name="Codex",
        signin_instructions="Sign in.",
    )
    assert find_unauthenticated_harness_reason(check) is not None


def test_antigravity_reads_its_signed_out_message_as_signed_out() -> None:
    """`agy models` prints this verbatim when signed out (verified against agy 1.1.16).

    Pinned as a real subprocess against the REGISTERED pattern, so a reworded probe or a
    wrong regex fails here rather than silently gating every antigravity create.
    """
    message = "Error: Please sign in to view available models. Launch the CLI without arguments to sign in."
    check = ANTIGRAVITY_AUTH_CHECK.model_copy_update(("command", ("echo", message)))
    reason = find_unauthenticated_harness_reason(check)
    assert reason is not None
    assert "Antigravity" in reason


def test_antigravity_ignores_a_nonzero_exit_without_its_message() -> None:
    """The probe fetches the catalog over the network, so a transient failure exits non-zero
    too. That must NOT read as signed out -- it would refuse a create for a user who is
    signed in, and agy does not need `models` to run."""
    check = ANTIGRAVITY_AUTH_CHECK.model_copy_update(("command", ("false",)))
    assert find_unauthenticated_harness_reason(check) is None
