"""Shared helpers for the guard tests each Modal app runs over its shipped source.

Used by modal_app_kit's own boundary test and by the guard tests of the apps
that ship it (import boundary, image-requirements drift, entrypoint logger
naming, per-function logging bootstrap). This module is a test utility: it is
excluded from the wheel and from the Modal source mount, so it never reaches a
deployed container.
"""

import ast
import importlib.util
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.modal_image_requirements import ImageRequirementsExportError
from imbue.imbue_common.modal_image_requirements import image_requirements_export_command
from imbue.imbue_common.modal_image_requirements import image_requirements_path
from imbue.modal_app_kit.source_mount import shipped_python_source_ignore

_EXPORT_TIMEOUT_SECONDS: Final[int] = 60


def imported_module_names(path: Path) -> set[str]:
    """The absolute module names imported by the file.

    Collects ``import X`` targets and level-0 ``from X import ...`` sources;
    relative imports stay within the package and are boundary-neutral, so they
    are ignored.
    """
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)
        else:
            pass
    return names


def is_module_within_package(module_name: str, package_name: str) -> bool:
    """Whether the module is the package itself or one of its submodules.

    Precise membership, unlike a bare ``startswith``: a name-prefix sibling
    such as ``imbue.modal_app_kit_extras`` is NOT within ``imbue.modal_app_kit``.
    """
    return module_name == package_name or module_name.startswith(package_name + ".")


def shipped_module_files(package_dir: Path) -> list[Path]:
    """The .py files under ``package_dir`` that ship into the container, per the real mount rule."""
    shipped = []
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(package_dir)
        if not shipped_python_source_ignore(relative):
            shipped.append(path)
    return shipped


def export_image_requirements(repo_root: Path, package_name: str) -> str:
    """Render the app's hash-locked image requirements from the committed uv.lock (offline)."""
    command = image_requirements_export_command(package_name)
    result = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_EXPORT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ImageRequirementsExportError(
            f"`{' '.join(command)}` failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def regenerate_image_requirements(repo_root: Path, package_names: tuple[str, ...]) -> list[Path]:
    """Rewrite each app's committed export from uv.lock; returns the written paths."""
    written_paths: list[Path] = []
    for package_name in package_names:
        export_path = image_requirements_path(repo_root, package_name)
        export_path.write_text(export_image_requirements(repo_root, package_name))
        written_paths.append(export_path)
    return written_paths


def _is_get_logger_callee(func: ast.expr) -> bool:
    """Whether the callee is ``getLogger``, called as ``logging.getLogger`` or imported by name."""
    if isinstance(func, ast.Attribute):
        return func.attr == "getLogger"
    return isinstance(func, ast.Name) and func.id == "getLogger"


def uses_dunder_name_logger(path: Path) -> bool:
    """Whether the file calls ``getLogger(__name__)`` anywhere (as ``logging.getLogger`` or by name).

    Modal mounts an app's entrypoint as the top-level module ``app``, so in the
    container ``__name__`` is ``"app"`` -- outside the ``imbue.*`` subtree the
    shared logging bootstrap opens to INFO. An entrypoint logger must therefore
    be named explicitly (``imbue.<package>.app``) or its INFO lines vanish.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_get_logger_callee(node.func)):
            continue
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(isinstance(argument, ast.Name) and argument.id == "__name__" for argument in arguments):
            return True
    return False


def _is_modal_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the function carries an ``@app.function(...)`` decorator (every entrypoint names its ``modal.App`` ``app``)."""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        callee = decorator.func
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "function"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "app"
        ):
            return True
    return False


def _first_statement_is_configure_logging(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = node.body[1:] if ast.get_docstring(node) is not None else node.body
    if not body or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
        return False
    callee = body[0].value.func
    return isinstance(callee, ast.Name) and callee.id == "configure_logging"


def modal_functions_missing_logging_bootstrap(path: Path) -> list[str]:
    """The names of the entrypoint's Modal functions whose first statement is not ``configure_logging()``.

    A container installs its JSON root handler only through that call, so a
    Modal function (web app, cron, or spawned function) that does not make it
    first drops every INFO line and prints WARNING+ as bare text -- and
    anything that fails before the call is logged the same way. The docstring
    does not count as a statement.
    """
    tree = ast.parse(path.read_text())
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _is_modal_function(node)
        and not _first_statement_is_configure_logging(node)
    ]


class ShippedImport(FrozenModel):
    """One import statement reachable from a Modal app's shipped source, for the import-boundary tests."""

    importer: str = Field(
        description=(
            "The shipped module that makes the import: its path relative to the mounted package's parent "
            "directory for the app's own modules, or its dotted module name when reached through another "
            "shipped package"
        )
    )
    module_name: str = Field(description="The absolute module name imported")
    is_target_mount_excluded: bool = Field(
        description=(
            "Whether the import names a module of a shipped package whose FILE the source-mount rule leaves out "
            "(tests, testing.py, conftest.py, the entrypoint) -- importable locally, absent in the container"
        )
    )


def _module_file_or_none(module_name: str) -> Path | None:
    """The .py file behind a module name in the deploying interpreter (a package's __init__.py), or None."""
    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        return None
    if spec is None:
        return None
    if spec.submodule_search_locations:
        init_path = Path(spec.submodule_search_locations[0]) / "__init__.py"
        return init_path if init_path.exists() else None
    if spec.origin is not None and spec.origin.endswith(".py"):
        return Path(spec.origin)
    return None


def _shipped_package_root_or_none(module_name: str, shipped_packages: Sequence[str]) -> Path | None:
    for package_name in shipped_packages:
        if is_module_within_package(module_name, package_name):
            spec = importlib.util.find_spec(package_name)
            if spec is not None and spec.submodule_search_locations:
                return Path(spec.submodule_search_locations[0])
    return None


def transitive_shipped_imports(package_dir: Path, shipped_packages: Sequence[str]) -> list[ShippedImport]:
    """Every import reachable from the package's shipped modules, following imports INTO other shipped packages.

    A shipped module may import another shipped package (``imbue.modal_app_kit``,
    ``imbue.imbue_common``); whatever THAT module imports must also exist in the
    container, so the walk resolves each such import to its file (via
    ``importlib.util.find_spec`` in the deploying interpreter, the same resolution
    the mount uses) and keeps going. Files the mount rule excludes are reported,
    not followed: importing them works locally and fails in the container.
    """
    shipped_imports: list[ShippedImport] = []
    visited: set[Path] = set()
    pending = [(str(path.relative_to(package_dir.parent)), path) for path in shipped_module_files(package_dir)]
    while pending:
        importer, path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        for module_name in sorted(imported_module_names(path)):
            is_target_mount_excluded = False
            shipped_root = _shipped_package_root_or_none(module_name, shipped_packages)
            target_path = _module_file_or_none(module_name) if shipped_root is not None else None
            if shipped_root is not None and target_path is not None:
                is_target_mount_excluded = shipped_python_source_ignore(target_path.relative_to(shipped_root))
                if not is_target_mount_excluded:
                    pending.append((module_name, target_path))
            shipped_imports.append(
                ShippedImport(
                    importer=importer, module_name=module_name, is_target_mount_excluded=is_target_mount_excluded
                )
            )
    return shipped_imports
