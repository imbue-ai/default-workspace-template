from types import ModuleType

import pytest

# Only stdlib-only package modules may be imported at the top of a conftest under this app: the
# ROOT pytest run descends every directory here (its ignore glob stops the files, not the
# directories) and loads each conftest it meets from a venv that has no harbor. Third-party
# imports are held to the same bar: loguru is in the root venv, harbor is not.
from imbue.minds_evals.template_loading import load_template_module


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
