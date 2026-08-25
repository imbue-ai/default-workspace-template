from collections.abc import Iterator

import pytest
import sentry_sdk

import imbue.remote_service_connector.auth as auth_mod
from imbue.imbue_common.conftest_hooks import register_conftest_hooks
from imbue.remote_service_connector.testing import hold_stable_download_link

register_conftest_hooks(globals())


@pytest.fixture
def isolated_sentry_client() -> Iterator[None]:
    """Clear the sentry-sdk global client around a test that installs or inspects one.

    Same shape as modal_app_kit's fixture of the same name (conftest fixtures
    are directory-scoped, so it cannot be shared across projects). The clear
    runs both before (isolating from any client a previous test left behind)
    and after (even when the test fails mid-assertion).
    """
    sentry_sdk.get_global_scope().set_client(None)
    yield
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture(autouse=True)
def _clear_paid_status_cache() -> None:
    """Drop the connector's process-global paid-status cache before each test.

    The cache lives at module scope, so without this a positive/negative
    entry from one test could bleed into another. Tests that exercise the
    cache set a positive TTL explicitly; everything else runs with it empty.
    """
    auth_mod.clear_paid_status_cache()


@pytest.fixture(autouse=True)
def _hold_a_stable_download_link() -> None:
    """Keep every test off the live update feed, and out of each other's cache.

    ``GET /download`` resolves the stable channel manifest over the network, so
    any test touching that route would otherwise fetch it for real. Holding
    "could not be read" makes the route serve its fallback; tests that care
    what the link resolves to hold their own.
    """
    hold_stable_download_link(None)


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
