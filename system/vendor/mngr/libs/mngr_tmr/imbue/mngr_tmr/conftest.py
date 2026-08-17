"""Test fixtures for mngr-test-mapreduce.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, etc.) and defines test-mapreduce-specific fixtures below.
"""

import json
from pathlib import Path

import pytest

from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr_tmr.prompts import TESTING_AGENT_OUTCOME_FILENAME
from imbue.mngr_tmr.report import EXTRACTED_TEST_OUTPUT_DIR

register_plugin_test_fixtures(globals())


def write_mapper_outcome(inputs_dir: Path, agent_name: str, payload: dict[str, object]) -> None:
    """Write a mapper outcome where every reader of the run layout expects it.

    Shared: pr_summary, escalation_coverage, and the report all read this same
    ``<dir>/<agent>/test_output/<outcome>.json`` layout, so the tests for them
    should not each carry their own copy of it.
    """
    target = inputs_dir / agent_name / EXTRACTED_TEST_OUTPUT_DIR / TESTING_AGENT_OUTCOME_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload))


@pytest.fixture
def localhost(local_provider: LocalProviderInstance) -> OnlineHostInterface:
    """Get a started localhost for tests that need to read/write files on a host."""
    host, _ = ensure_host_started(
        local_provider.get_host(HostName(LOCAL_HOST_NAME)), is_start_desired=True, provider=local_provider
    )
    return host
