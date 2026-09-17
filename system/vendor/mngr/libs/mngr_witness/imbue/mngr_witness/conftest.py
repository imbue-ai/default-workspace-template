from pathlib import Path

import pytest

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())


@pytest.fixture()
def repo_root() -> Path:
    """The monorepo checkout that holds this package; the committed spec artifacts live relative to it."""
    return Path(__file__).resolve().parents[4]
