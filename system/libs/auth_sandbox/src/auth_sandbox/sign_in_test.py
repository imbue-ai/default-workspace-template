from auth_sandbox.mock_latchkey_test import FakeLatchkey
from auth_sandbox.sign_in import run_sign_in


def test_run_sign_in_initializes_then_runs_the_browser_flow_for_the_account() -> None:
    latchkey = FakeLatchkey()
    outcome = run_sign_in(latchkey, "slack", "me@example.com")
    assert outcome.is_stored is True
    assert outcome.detail == ""
    assert latchkey.initialized_count == 1
    assert latchkey.browser_calls == [("slack", "me@example.com")]


def test_run_sign_in_passes_no_account_when_adding_one_and_reports_latchkeys_reason() -> (
    None
):
    latchkey = FakeLatchkey(
        is_sign_in_stored=False, sign_in_detail="Login cancelled by the user"
    )
    outcome = run_sign_in(latchkey, "github", None)
    assert outcome.is_stored is False
    assert outcome.detail == "Login cancelled by the user"
    assert latchkey.browser_calls == [("github", None)]
