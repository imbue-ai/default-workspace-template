import sys
from pathlib import Path
from types import ModuleType

from imbue.modal_app_kit.testing import imported_module_names
from imbue.modal_app_kit.testing import is_module_within_package


def test_is_connection_failure_output_matches_only_connection_error_codes(app_module: ModuleType) -> None:
    assert app_module._is_connection_failure_output(
        "Error: P1001: Can't reach database server at `ep-abc-pooler.neon.tech:5432`"
    )
    assert app_module._is_connection_failure_output("Error: P1002: The database server was reached but timed out.")
    assert app_module._is_connection_failure_output("Error: P1017: Server has closed the connection.")
    assert not app_module._is_connection_failure_output("Error: P3018: A migration failed to apply.")
    assert not app_module._is_connection_failure_output("The database schema is not in sync with your Prisma schema.")


def test_entrypoint_imports_only_shipped_dependencies() -> None:
    """app.py runs in a container that has only its pip-installed set plus the
    imbue.modal_app_kit source mount; any other monorepo import would pass
    locally and crash the deployed container at import time."""
    allowed_roots = {"modal", "tenacity", "yaml", "litellm", "prisma"}
    allowed_imbue_package = "imbue.modal_app_kit"
    violations: list[str] = []
    for module_name in imported_module_names(Path(__file__).parent / "app.py"):
        root = module_name.split(".")[0]
        if root in sys.stdlib_module_names or root in allowed_roots:
            continue
        if root == "imbue" and is_module_within_package(module_name, allowed_imbue_package):
            continue
        violations.append(module_name)
    assert violations == []
