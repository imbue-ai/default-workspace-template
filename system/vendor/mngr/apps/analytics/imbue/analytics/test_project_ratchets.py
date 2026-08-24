"""Project-specific guardrails for the analytics app's Modal deployment model.

The container only receives the pip set from the image dependency group plus
the source mounts for ``imbue.analytics`` and ``imbue.modal_app_kit`` --
nothing else from the monorepo exists at runtime. An import that violates
these rules passes every local test and then crashes the deployed container at
import time, so the boundary is enforced here. See
libs/modal_app_kit/README.md for the full deployment model.
"""

import ast
import sys
import tomllib
from pathlib import Path

from imbue.analytics.deploy_constants import THIRD_PARTY_IMPORT_ROOTS
from imbue.modal_app_kit.testing import imported_module_names
from imbue.modal_app_kit.testing import is_module_within_package
from imbue.modal_app_kit.testing import shipped_module_files

_PACKAGE_DIR = Path(__file__).parent

# Import roots every shipped module may use, beyond the stdlib. "imbue" is
# constrained to the shipped subpackages by _SHIPPED_IMBUE_PACKAGES below.
_ALLOWED_ROOTS = THIRD_PARTY_IMPORT_ROOTS | {"imbue"}
_SHIPPED_IMBUE_PACKAGES = ("imbue.analytics", "imbue.modal_app_kit")

# The injected collection entrypoint ships in the container as FILE CONTENT
# only (the runner writes it into workspaces); nothing in the container ever
# imports it, and its third-party imports resolve from its own PEP 723 script
# environment instead of the image. It is therefore exempt from the image
# import boundary and held to its own header by
# test_injected_entrypoint_declares_its_script_dependencies below.
_INJECTED_ENTRYPOINT_RELPATH = Path("injected") / "collect.py"

# Import roots the OTHER injected modules may use: they must stay importable
# inside the script environment (stdlib + the script's declared deps), which
# is a strictly tighter bound than the image's.
_INJECTED_MODULE_ALLOWED_ROOTS = frozenset({"pydantic"})
_INJECTED_PACKAGE = "imbue.analytics.injected"

# Runtime seams that tests replace via the owning module. A cross-module
# ``from x import seam`` binds the function object at import time and silently
# escapes the substitution, so cross-module callers must reference these
# through the module attribute (``module.seam(...)``) instead.
_MODULE_ATTRIBUTE_SEAMS = ("get_ops_db_connection",)


def test_shipping_rule_actually_selects_the_production_modules() -> None:
    """Guard the guard: the mount rule must ship app-adjacent modules and exclude this test."""
    shipped_names = {str(p.relative_to(_PACKAGE_DIR)) for p in shipped_module_files(_PACKAGE_DIR)}
    assert "jobs.py" in shipped_names
    assert "aggregation.py" in shipped_names
    assert "app.py" not in shipped_names
    assert "testing.py" not in shipped_names
    assert not any(name.endswith("_test.py") or name.startswith("test_") for name in shipped_names)


def test_shipped_modules_import_only_shipped_dependencies() -> None:
    """Every shipped module imports only stdlib, the pip-installed set, or shipped packages."""
    violations: list[str] = []
    for path in shipped_module_files(_PACKAGE_DIR):
        if path.relative_to(_PACKAGE_DIR) == _INJECTED_ENTRYPOINT_RELPATH:
            continue
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


def _read_script_header_dependencies(script_path: Path) -> list[str]:
    """Parse the PEP 723 ``# /// script`` block and return its dependency specifiers."""
    lines = script_path.read_text().splitlines()
    block_lines: list[str] = []
    is_inside_block = False
    for line in lines:
        if line.strip() == "# /// script":
            is_inside_block = True
        elif line.strip() == "# ///" and is_inside_block:
            break
        elif is_inside_block:
            block_lines.append(line.removeprefix("#").removeprefix(" "))
        else:
            pass
    parsed = tomllib.loads("\n".join(block_lines))
    return [str(dep) for dep in parsed.get("dependencies", [])]


def test_injected_entrypoint_declares_its_script_dependencies() -> None:
    """collect.py's third-party imports must all be pinned in its PEP 723 header.

    The entrypoint never runs in the container (the runner injects it into
    workspaces and executes it under ``uv run --script``), so its imports are
    governed by the script header, not the image. Every non-stdlib,
    non-injected-package import must map to a ``==``-pinned header dependency.
    """
    script_path = _PACKAGE_DIR / _INJECTED_ENTRYPOINT_RELPATH
    dependencies = _read_script_header_dependencies(script_path)
    assert dependencies, "collect.py must carry a PEP 723 dependency block"
    # A dependency counts as pinned when it names an exact version or an exact
    # artifact URL (the spacy model wheel).
    unpinned = [dep for dep in dependencies if "==" not in dep and "@" not in dep]
    assert not unpinned, f"Script dependencies must be ==-pinned for a deterministic environment: {unpinned}"
    declared_names = {dep.split("==")[0].split("@")[0].strip().lower().replace("_", "-") for dep in dependencies}
    violations: list[str] = []
    for module_name in imported_module_names(script_path):
        root = module_name.split(".")[0]
        if root in sys.stdlib_module_names:
            continue
        if is_module_within_package(module_name, _INJECTED_PACKAGE):
            continue
        if root.lower().replace("_", "-") not in declared_names:
            violations.append(module_name)
    assert not violations, f"collect.py imports modules its PEP 723 header does not declare: {violations}"


def test_injected_modules_stay_importable_inside_the_script_environment() -> None:
    """The injected sibling modules may import only the stdlib, pydantic, and each other.

    They are injected next to collect.py and imported inside its isolated
    script environment, so any other import would crash collection inside
    workspaces while passing every local test.
    """
    violations: list[str] = []
    injected_dir = _PACKAGE_DIR / "injected"
    for path in sorted(injected_dir.glob("*.py")):
        is_test_file = path.name.endswith("_test.py") or path.name.startswith("test_")
        if is_test_file or path.relative_to(_PACKAGE_DIR) == _INJECTED_ENTRYPOINT_RELPATH:
            continue
        for module_name in imported_module_names(path):
            root = module_name.split(".")[0]
            if root in sys.stdlib_module_names or root in _INJECTED_MODULE_ALLOWED_ROOTS:
                continue
            if is_module_within_package(module_name, _INJECTED_PACKAGE):
                continue
            violations.append(f"{path.name}: {module_name}")
    assert not violations, f"Injected modules import things the script environment does not have: {violations}"


def test_shipped_modules_never_import_the_entrypoint() -> None:
    """app.py is excluded from the source mount, so importing it only fails in production."""
    violations = [
        path.name
        for path in shipped_module_files(_PACKAGE_DIR)
        if any(m == "imbue.analytics.app" for m in imported_module_names(path))
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


def test_collection_runner_log_lines_never_reference_payload_variables() -> None:
    """The runner's logs flow to the ops telemetry store; payload content must never enter them.

    A reminder ratchet in the spirit of the disclosure: any logging call in
    the runner-side modules that interpolates a payload-carrying variable
    (record payloads, script stdout/stderr, message text) is a leak. Log ids,
    outcomes, counts, and durations instead.
    """
    forbidden_argument_names = {"payload", "stdout_text", "stderr_tail", "stdout", "content", "message_text"}
    violations: list[str] = []
    for module_name in ("collection.py", "protocol.py"):
        tree = ast.parse((_PACKAGE_DIR / module_name).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            is_logger_call = isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"
            if not is_logger_call:
                continue
            for argument in ast.walk(node):
                if isinstance(argument, ast.Name) and argument.id in forbidden_argument_names:
                    violations.append(f"{module_name}:{node.lineno}: logs {argument.id!r}")
    assert not violations, f"Runner log lines must never carry payload content: {violations}"


def test_runtime_seams_are_referenced_through_their_module() -> None:
    """Cross-module seam calls must be late-bound (module attribute), never from-imported."""
    violations: list[str] = []
    for path in shipped_module_files(_PACKAGE_DIR):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom):
                continue
            is_package_internal = node.level > 0 or (
                node.module is not None and is_module_within_package(node.module, "imbue.analytics")
            )
            if not is_package_internal:
                continue
            for alias in node.names:
                if alias.name in _MODULE_ATTRIBUTE_SEAMS:
                    violations.append(f"{path.name}: from {'.' * node.level}{node.module or ''} import {alias.name}")
    assert not violations, (
        "Runtime seams must be called through their owning module "
        f"(e.g. ``ops_db.get_ops_db_connection(...)``), not from-imported: {violations}"
    )
