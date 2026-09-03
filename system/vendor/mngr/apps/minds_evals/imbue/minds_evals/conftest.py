from types import ModuleType

import pytest

# Only stdlib-only package modules may be imported at the top of a conftest under this app: the
# ROOT pytest run descends every directory here (its ignore glob stops the files, not the
# directories) and loads each conftest it meets from a venv that has no harbor.
from imbue.minds_evals.template_loading import load_template_module


@pytest.fixture(scope="session")
def gate_checks() -> ModuleType:
    """The structural-gate module that ships into every generated dataset, loaded from its path.

    It lives under `templates/` and runs in the verifier container against fixed absolute paths, so
    it is not importable as part of this package. Tests exercise the pure predicates behind its
    criteria; nothing mutates the module, so one load serves the whole session.
    """
    return load_template_module("tests/gates/checks.py", "minds_evals_gate_checks")


@pytest.fixture(scope="session")
def wordiness_guard() -> ModuleType:
    """The wordiness-guard module that ships into every generated dataset, loaded from its path the
    way `gate_checks` is: it too runs in the verifier container against fixed absolute paths."""
    return load_template_module("tests/quality/wordiness.py", "minds_evals_wordiness_guard")
