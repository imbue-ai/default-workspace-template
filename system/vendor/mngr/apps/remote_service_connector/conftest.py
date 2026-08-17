import pytest

import imbue.remote_service_connector.auth as auth_mod
from imbue.imbue_common.conftest_hooks import register_conftest_hooks

register_conftest_hooks(globals())


@pytest.fixture(autouse=True)
def _clear_paid_status_cache() -> None:
    """Drop the connector's process-global paid-status cache before each test.

    The cache lives at module scope, so without this a positive/negative
    entry from one test could bleed into another. Tests that exercise the
    cache set a positive TTL explicitly; everything else runs with it empty.
    """
    auth_mod.clear_paid_status_cache()


@pytest.fixture(autouse=True)
def _run_tests_as_a_dev_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the deploy tier to a dev env name for every test.

    ``read_deploy_env`` deliberately defaults to "production" when
    MNGR_DEPLOY_ENV is unset (a bare ``modal deploy`` fails closed), which
    would make the tier-restricted behaviors (e.g. the JSON signup refusal)
    fire in every test. Tests that exercise those behaviors set the tier
    explicitly; everything else runs as a dev env.
    """
    monkeypatch.setenv("MNGR_DEPLOY_ENV", "dev-connector-tests")
