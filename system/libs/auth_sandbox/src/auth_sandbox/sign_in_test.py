from auth_sandbox.mock_latchkey_test import FakeLatchkey
from auth_sandbox.sign_in import (
    account_for_browser_sign_in,
    offers_browser_sign_in,
    run_sign_in,
)


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


def test_only_a_service_that_lists_its_options_without_the_browser_takes_typed_credentials() -> (
    None
):
    assert offers_browser_sign_in({"authOptions": ["browser", "set"]}) is True
    assert offers_browser_sign_in({"authOptions": ["set"]}) is False
    # A service latchkey does not know, or one described without options, signs in through the browser as on
    # the desktop.
    assert offers_browser_sign_in({}) is True
    assert offers_browser_sign_in({"authOptions": []}) is True
    assert offers_browser_sign_in({"authOptions": "set"}) is True


def test_only_a_service_that_cannot_detect_the_login_account_is_named_one() -> None:
    assert (
        account_for_browser_sign_in(
            {"capabilities": {"detectsLoginAccount": False}}, "work"
        )
        == "work"
    )
    assert (
        account_for_browser_sign_in(
            {"capabilities": {"detectsLoginAccount": True}}, "work"
        )
        is None
    )
    assert account_for_browser_sign_in({}, "work") is None
    assert (
        account_for_browser_sign_in(
            {"capabilities": {"detectsLoginAccount": False}}, None
        )
        is None
    )
