import os
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

# Every import at the top of a conftest under this app must resolve in the ROOT venv: the root
# pytest run descends every directory here (its ignore glob stops the files, not the directories)
# and loads each conftest it meets from that venv, which has no harbor. That admits the stdlib, the
# third-party packages the root workspace holds (loguru, playwright, pydantic), the monorepo's own
# libraries, and the modules of this package that reach none of harbor -- and nothing else.
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds_evals import flow_browser
from imbue.minds_evals.template_loading import load_template_module


@pytest.fixture(scope="session")
def chromium_path() -> Path:
    """The Chromium the flow lab launches: playwright's, resolved the way the box resolves its own.

    A machine without it skips the lab's tests and says how to install it; CI fails them instead,
    because there the browser is installed by the workflow and its absence is a broken job, not a
    developer who has not run the install yet.
    """
    path = flow_browser.resolve_chromium_path()
    if not path.exists():
        message = flow_browser.missing_chromium_message(path)
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return path


@pytest.fixture
def flow_lab_group() -> Iterator[ConcurrencyGroup]:
    """The group that owns a test's browser and step script processes, so none outlives the test."""
    with ConcurrencyGroup(name="flow-lab-test") as group:
        yield group


@pytest.fixture
def local_browser(chromium_path: Path, tmp_path: Path, flow_lab_group: ConcurrencyGroup) -> Iterator[str]:
    """A headless Chromium on a profile of its own, torn down with the test. Yields its CDP endpoint."""
    with flow_browser.launch_local_browser(
        chromium_path, tmp_path / "chromium-profile", flow_lab_group
    ) as cdp_endpoint_url:
        yield cdp_endpoint_url


@pytest.fixture(scope="session")
def gate_checks() -> ModuleType:
    """The structural-gate module that ships into every generated dataset, loaded from its path.

    It lives under `templates/` and runs in the verifier container against fixed absolute paths, so
    it is not importable as part of this package. Tests exercise the pure predicates behind its
    criteria; nothing mutates the module, so one load serves the whole session.
    """
    return load_template_module("tests/verifier/gates/checks.py", "minds_evals_gate_checks")


@pytest.fixture(scope="session")
def message_length_guard() -> ModuleType:
    """The message-length guard module that ships into every generated dataset, loaded from its path the
    way `gate_checks` is: it too runs in the verifier container against fixed absolute paths."""
    return load_template_module("tests/verifier/quality/message_lengths.py", "minds_evals_message_length_guard")


@pytest.fixture(scope="session")
def harness_report_renderer() -> ModuleType:
    """The harness-report renderer that ships into every generated dataset, loaded from its path the
    same way as the other verifier-container scripts."""
    return load_template_module("tests/verifier/render_harness_report.py", "minds_evals_harness_report")


@pytest.fixture(scope="session")
def harness_checks() -> ModuleType:
    """The harness_quality programmatic criteria that ship into every generated dataset."""
    return load_template_module("tests/verifier/harness_quality/checks.py", "minds_evals_harness_checks")


@pytest.fixture(scope="session")
def finalize() -> ModuleType:
    """The reward-composition script that ships into every generated dataset."""
    return load_template_module("tests/verifier/finalize.py", "minds_evals_finalize")
