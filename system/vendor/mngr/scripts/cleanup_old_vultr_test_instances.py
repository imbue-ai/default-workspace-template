#!/usr/bin/env python3
"""Script to clean up old Vultr test VPS instances.

This script is run by a CI job (on every push to main and pull request) to
destroy Vultr instances created by the mngr_vultr release tests that were left
behind when a test session was killed before its in-process cleanup
(``pytest_sessionfinish`` in ``libs/mngr_vultr/imbue/mngr_vultr/conftest.py``)
could run. It selects instances by the ``mngr-vultr-test-created=<timestamp>``
tag that conftest attaches at create time and destroys those older than
``--max-age-hours``. Production VPSes never carry that tag, so they are never
touched.

Skips silently (exit 0) when ``VULTR_API_KEY`` is unset, so it is safe to wire
into CI before the secret is configured.

Usage:
    uv run python scripts/cleanup_old_vultr_test_instances.py [--max-age-hours HOURS]

Options:
    --max-age-hours  Maximum age in hours for test instances to keep (default: 1.0)
"""

import argparse
import os
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from pydantic import SecretStr
from tenacity import retry
from tenacity import retry_if_exception
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.imbue_common.logging import setup_logging
from imbue.mngr_vps.errors import VpsApiError
from imbue.mngr_vultr.cleanup import cleanup_old_vultr_test_instances
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.testing import VULTR_TEST_OS_ID


def _is_transient_vps_api_error(exception: BaseException) -> bool:
    # 5xx is a Vultr-side failure and status 0 is the client's marker for a
    # network-level request failure; both are worth retrying. 4xx (bad key,
    # bad request) is deterministic and must fail immediately.
    return isinstance(exception, VpsApiError) and (exception.status_code >= 500 or exception.status_code == 0)


# The whole cleanup pass is idempotent (list tagged instances, destroy the old
# ones; re-listing after a partial pass just sees fewer), so retrying the full
# pass on a transient provider error is safe.
@retry(
    retry=retry_if_exception(_is_transient_vps_api_error),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _cleanup_with_retry(client: VultrVpsClient, max_age: timedelta) -> int:
    return cleanup_old_vultr_test_instances(
        client,
        max_age=max_age,
        now=datetime.now(timezone.utc),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up old Vultr test VPS instances",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for test instances to keep (default: 1.0)",
    )
    args = parser.parse_args()

    setup_logging(level="INFO")

    api_key = os.environ.get("VULTR_API_KEY", "")
    if not api_key:
        print("VULTR_API_KEY not set; skipping Vultr test instance cleanup")
        return 0

    client = VultrVpsClient(api_key=SecretStr(api_key), os_id=VULTR_TEST_OS_ID)
    cleaned_count = _cleanup_with_retry(client, max_age=timedelta(hours=args.max_age_hours))

    if cleaned_count > 0:
        print(f"Cleaned up {cleaned_count} old Vultr test instance(s)")
    else:
        print("No old Vultr test instances found to clean up")

    return 0


if __name__ == "__main__":
    sys.exit(main())
