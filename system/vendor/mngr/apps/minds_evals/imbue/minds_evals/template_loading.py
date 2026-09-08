"""Load one of the scripts under `templates/` from its file path.

Kept stdlib-only, apart from nothing: the conftest files under this app import it, and the root
pytest run loads those conftests from a venv that has no harbor, which nearly every other module in
this package imports. Anything added here that reaches the rest of the package breaks every
root-level test run at collection.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Final

# The template tree as it sits in the checkout, which is the same content `generate.py` copies into
# a task directory.
TEMPLATES_DIR: Final[Path] = Path(__file__).parent / "templates"


def load_template_module(relative_path: str, module_name: str) -> ModuleType:
    """One script under `templates/`, loaded from its path.

    These scripts ship into the verifier container as self-contained stdlib files, not as package
    modules, so they cannot be imported as `imbue.minds_evals.templates...`. Loading the very file
    the dataset ships is what makes a host-side test of one meaningful. Each call executes the
    module afresh and registers nothing in `sys.modules`, so callers that want a clean module per
    test get one.
    """
    spec = importlib.util.spec_from_file_location(module_name, TEMPLATES_DIR / relative_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
