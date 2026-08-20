from collections.abc import Iterator

import pytest
import sentry_sdk


@pytest.fixture
def isolated_sentry_client() -> Iterator[None]:
    """Clear the sentry-sdk global client around a test that installs or inspects one.

    The clear runs both before (isolating from any client a previous test
    left behind) and after (even when the test fails mid-assertion, which an
    inline trailing reset would miss).
    """
    sentry_sdk.get_global_scope().set_client(None)
    yield
    sentry_sdk.get_global_scope().set_client(None)
