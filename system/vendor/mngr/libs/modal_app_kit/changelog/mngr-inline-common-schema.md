Test-infrastructure only; no change to shipped behavior.

`sentry_sdk.init` imports every default integration on its first call in a process -- a cascade of roughly 3000 modules. Whichever sentry test ran first in a worker paid that inside its own 10s budget, and on a cold CI sandbox it intermittently timed out. A session-scoped fixture in `conftest.py` now performs that import once; the package sets `timeout_func_only`, so fixture time is not charged to any test.

The three tests that call `sentry_sdk.init` are also marked `@pytest.mark.flaky`, which moves them into the offload group that retries, so a slow sandbox does not fail the run outright.
