"""Shared helpers for import-boundary guard tests.

Used by modal_app_kit's own boundary test and by the guard tests of the apps
that ship it (remote_service_connector, modal_litellm). This module is a test
utility: it is excluded from the wheel and from the Modal source mount, so it
never reaches a deployed container.
"""

import ast
import subprocess
from pathlib import Path
from typing import Final

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
