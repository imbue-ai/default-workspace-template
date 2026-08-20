"""Project-specific guardrail: modal_app_kit ships into Modal containers as a source mount.

The consuming apps' containers have only their own pip-installed packages, so
this library must stay importable with nothing beyond the stdlib and the modal
SDK (which Modal injects into every container). Anything more would crash the
consuming apps at import time in production while passing every local test.
"""

import sys
from pathlib import Path

from imbue.modal_app_kit.testing import imported_module_names
from imbue.modal_app_kit.testing import shipped_module_files

_PACKAGE_DIR = Path(__file__).parent

_ALLOWED_NON_STDLIB_ROOTS = frozenset({"modal"})


def test_shipped_modules_import_only_stdlib_and_modal() -> None:
    violations: list[str] = []
    for path in shipped_module_files(_PACKAGE_DIR):
        for module_name in imported_module_names(path):
            root = module_name.split(".")[0]
            if root in sys.stdlib_module_names or root in _ALLOWED_NON_STDLIB_ROOTS:
                continue
            violations.append(f"{path.name}: {module_name}")
    assert not violations, (
        "modal_app_kit ships into Modal containers as source; it may import only "
        f"the stdlib and the modal SDK: {violations}"
    )


def test_shipping_rule_selects_the_production_modules() -> None:
    shipped_names = {str(p.relative_to(_PACKAGE_DIR)) for p in shipped_module_files(_PACKAGE_DIR)}
    assert "deploy.py" in shipped_names
    assert "source_mount.py" in shipped_names
    assert not any(name.endswith("_test.py") or name.startswith("test_") for name in shipped_names)
