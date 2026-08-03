import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_THIS_DIR = Path(__file__).parent


@pytest.fixture(scope="session")
def app_module() -> ModuleType:
    """Load app.py as a module (it is deployed standalone and is not an importable package)."""
    app_path = _THIS_DIR / "app.py"
    spec = importlib.util.spec_from_file_location("modal_litellm_app_under_test", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
