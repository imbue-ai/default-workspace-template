"""Project-specific guardrails for the redirector's Modal deployment model.

The container only receives the pip set in ``deploy_constants`` plus the
source mounts for ``imbue.oauth_redirector`` and ``imbue.modal_app_kit`` --
nothing else from the monorepo exists at runtime. See
libs/modal_app_kit/README.md for the full deployment model.
"""

import sys
from pathlib import Path

from imbue.modal_app_kit.testing import imported_module_names
from imbue.modal_app_kit.testing import is_module_within_package
from imbue.modal_app_kit.testing import modal_functions_missing_logging_bootstrap
from imbue.modal_app_kit.testing import shipped_module_files
from imbue.modal_app_kit.testing import uses_dunder_name_logger
from imbue.oauth_redirector.deploy_constants import THIRD_PARTY_IMPORT_ROOTS

_PACKAGE_DIR = Path(__file__).parent

_ALLOWED_ROOTS = THIRD_PARTY_IMPORT_ROOTS | {"imbue"}
_SHIPPED_IMBUE_PACKAGES = ("imbue.oauth_redirector", "imbue.modal_app_kit")


def test_shipping_rule_actually_selects_the_production_modules() -> None:
    """Guard the guard: the mount rule must ship the web module and exclude this test."""
    shipped_names = {str(p.relative_to(_PACKAGE_DIR)) for p in shipped_module_files(_PACKAGE_DIR)}
    assert "web.py" in shipped_names
    assert "forwarding.py" in shipped_names
    assert "app.py" not in shipped_names
    assert not any(name.endswith("_test.py") or name.startswith("test_") for name in shipped_names)


def test_shipped_modules_import_only_shipped_dependencies() -> None:
    """Every shipped module imports only stdlib, the pip-installed set, or shipped packages."""
    violations: list[str] = []
    for path in shipped_module_files(_PACKAGE_DIR):
        for module_name in imported_module_names(path):
            root = module_name.split(".")[0]
            if root in sys.stdlib_module_names:
                continue
            if root == "imbue":
                if not any(is_module_within_package(module_name, pkg) for pkg in _SHIPPED_IMBUE_PACKAGES):
                    violations.append(f"{path.name}: {module_name}")
                continue
            if root not in _ALLOWED_ROOTS:
                violations.append(f"{path.name}: {module_name}")
    assert not violations, (
        "Shipped modules import packages that do not exist in the deployed container "
        f"(fix the import, or add the dependency to deploy_constants + the image): {violations}"
    )


def test_shipped_modules_never_import_the_entrypoint() -> None:
    """app.py is excluded from the source mount, so importing it only fails in production."""
    violations = [
        path.name
        for path in shipped_module_files(_PACKAGE_DIR)
        if any(m == "imbue.oauth_redirector.app" for m in imported_module_names(path))
    ]
    assert not violations, f"Shipped modules must never import the app entrypoint: {violations}"


def test_only_the_entrypoint_imports_modal() -> None:
    """Deployment concerns stay in app.py; shipped modules must not touch the modal SDK."""
    violations = [
        path.name
        for path in shipped_module_files(_PACKAGE_DIR)
        if any(m == "modal" or m.startswith("modal.") for m in imported_module_names(path))
    ]
    assert not violations, f"Only app.py may import modal: {violations}"


def test_entrypoint_logger_is_named_under_imbue() -> None:
    """In the container the entrypoint is module ``app``, so a ``__name__`` logger would drop its INFO lines."""
    assert not uses_dunder_name_logger(_PACKAGE_DIR / "app.py")


def test_every_modal_function_bootstraps_logging_first() -> None:
    """The JSON root handler exists only once ``configure_logging()`` runs; a function that skips it drops its INFO lines."""
    assert modal_functions_missing_logging_bootstrap(_PACKAGE_DIR / "app.py") == []
