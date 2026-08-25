"""Project-specific guardrails for the connector's Modal deployment model.

The container only receives the packages listed in ``deploy_constants`` plus
the source mounts for ``imbue.remote_service_connector`` and
``imbue.modal_app_kit`` -- nothing else from the monorepo exists at runtime.
An import that violates these rules passes every local test and then crashes
the deployed container at import time, so the boundary is enforced here.
See libs/modal_app_kit/README.md for the full deployment model.
"""

import ast
import sys
from pathlib import Path

from imbue.modal_app_kit.testing import imported_module_names
from imbue.modal_app_kit.testing import is_module_within_package
from imbue.modal_app_kit.testing import shipped_module_files
from imbue.remote_service_connector.deploy_constants import THIRD_PARTY_IMPORT_ROOTS

_PACKAGE_DIR = Path(__file__).parent

# Import roots every shipped module may use, beyond the stdlib. "imbue" is
# constrained to the shipped subpackages by _SHIPPED_IMBUE_PACKAGES below.
_ALLOWED_ROOTS = THIRD_PARTY_IMPORT_ROOTS | {"imbue"}
_SHIPPED_IMBUE_PACKAGES = ("imbue.remote_service_connector", "imbue.modal_app_kit")

# Runtime seams that tests replace via monkeypatch on the owning module. A
# cross-module ``from x import seam`` binds the function object at import time
# and silently escapes the patch, so cross-module callers must reference these
# through the module attribute (``module.seam(...)``) instead.
_MODULE_ATTRIBUTE_SEAMS = (
    "get_cloudflare_ctx",
    "get_pool_db_connection",
    "get_entitlements_store",
    "get_key_store",
    "get_grant_store",
    "get_sync_store",
    "get_orphan_bucket_store",
    "litellm_request",
    "get_user_id_from_access_token",
    "resolve_account_email",
    "get_backfill_email",
    "issue_share_certificate",
    "get_accounts_oauth_provider",
    "get_browser_session_identity",
    "_sdk_create_browser_session",
    "_sdk_get_browser_session",
    "_verify_turnstile_token",
    "get_device_code_store",
    "_client_ip",
    "delete_user",
    "get_signup_attempt_store",
    "get_ip_reputation_cache",
    "get_ip_reputation_provider",
    "get_tor_exit_list",
)


def test_shipping_rule_actually_selects_the_production_modules() -> None:
    """Guard the guard: the mount rule must ship app-adjacent modules and exclude this test."""
    shipped_names = {str(p.relative_to(_PACKAGE_DIR)) for p in shipped_module_files(_PACKAGE_DIR)}
    assert "web.py" in shipped_names
    assert "r2/buckets.py" in shipped_names
    assert "app.py" not in shipped_names
    assert "testing.py" not in shipped_names
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
        if any(m == "imbue.remote_service_connector.app" for m in imported_module_names(path))
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


def test_runtime_seams_are_referenced_through_their_module() -> None:
    """Cross-module seam calls must be late-bound (module attribute), never from-imported.

    Checked on the AST so that aliasing (``import get_key_store as _get_key_store``)
    and relative imports cannot early-bind a seam unnoticed.
    """
    violations: list[str] = []
    for path in shipped_module_files(_PACKAGE_DIR):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom):
                continue
            is_package_internal = node.level > 0 or (
                node.module is not None and is_module_within_package(node.module, "imbue.remote_service_connector")
            )
            if not is_package_internal:
                continue
            for alias in node.names:
                if alias.name in _MODULE_ATTRIBUTE_SEAMS:
                    violations.append(f"{path.name}: from {'.' * node.level}{node.module or ''} import {alias.name}")
    assert not violations, (
        "Runtime seams must be called through their owning module "
        f"(e.g. ``stores.get_key_store()``), not from-imported: {violations}"
    )
