import pytest

from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())


@pytest.fixture(autouse=True)
def _run_tests_as_a_dev_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deploy tier to a dev env name for every test.

    ``read_deploy_env`` deliberately defaults to "production" when
    MNGR_DEPLOY_ENV is unset (a bare ``modal deploy`` fails closed); tests
    should never construct production-named Modal objects.
    """
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "dev-analytics-tests")
