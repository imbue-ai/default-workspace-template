"""End-to-end check that the connector's error reporting works on a real ci env.

Drives the connector's ``GET /health/reporting-probe`` (active on dev/ci
tiers only), which exercises every reporting channel in one request: a
metric log line, a warning-level Bugsink event, and an unmapped exception
through the app-level 500 handler. The assertable surface from outside is
the ``internal_error`` response contract -- in particular a well-formed,
non-empty ``event_id``, which proves the tier's ``RSC_SENTRY_DSN`` was
plumbed through the ``sentry`` Modal secret and the SDK actively captured
the event (an unconfigured or disabled SDK yields an empty ``event_id``,
failing this test loudly, which is exactly the regression it exists to
catch).

Store-side delivery is deliberately not asserted here: Bugsink's REST API
and OpenObserve's query API are loopback-only by design (SSH tunnel +
Vault-held tokens), so verifying the events landed is an operator action --
the unique ``marker`` in the probe's warning and exception text is there to
make that lookup trivial after a run.
"""

import re
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest

from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_HTTP_TIMEOUT_SECONDS = 60.0

# Sentry event ids are 32 lowercase hex chars (uuid4().hex).
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@pytest.mark.timeout(120)
def test_reporting_probe_returns_the_internal_error_contract_with_a_real_event_id(
    shared_env: Callable[[str], SharedEnvHandle],
) -> None:
    """One probe call proves the unexpected-exception reporting path end to end."""
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    marker = f"probe-{uuid4().hex}"

    resp = httpx.get(
        f"{connector_url}/health/reporting-probe",
        params={"marker": marker},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "internal_error"
    assert detail["message"]
    # ci tiers expose the exception repr; the marker ties any Bugsink event
    # back to this exact run for the operator's store-side lookup.
    assert marker in detail["exception"]
    assert "ReportingProbeError" in detail["exception"]
    # The load-bearing assertion: a well-formed event id proves the DSN was
    # plumbed and the SDK captured the exception. Empty means reporting is
    # broken (missing/empty DSN, kill switch, or init regression).
    assert _EVENT_ID_RE.fullmatch(detail["event_id"]), (
        f"expected a sentry event id, got {detail['event_id']!r} -- the ci env is not reporting to Bugsink "
        "(RSC_SENTRY_DSN unset/empty in the sentry secret, MINDS_SENTRY_DISABLED=1, or an init_sentry regression)"
    )
